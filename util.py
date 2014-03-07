import re
import os
import datetime
import pymongo
import requests
import json
import thread
import time

from rauth.service import OAuth1Service, OAuth1Session
from elementtree import ElementTree
from bs4 import BeautifulSoup

mongo_client = pymongo.MongoClient(os.environ['MONGOHQ_URL'])
mongo_db = mongo_client[os.environ['DB_NAME']]

mongo_users = mongo_db['users']
mongo_users.ensure_index([("user_id", pymongo.ASCENDING)], unique=True)

mongo_pending_oauth = mongo_db['pending_oauth']
mongo_pending_oauth.ensure_index([("created", pymongo.ASCENDING)], expireAfterSeconds=3600)

mongo_book_details = mongo_db['books']
mongo_book_details.ensure_index([("isbn13", pymongo.ASCENDING)], unique=True)

mongo_sfpl_books = mongo_db['sfpl_books']
mongo_sfpl_books.ensure_index([("isbn13", pymongo.ASCENDING)], unique=True)
mongo_sfpl_books.ensure_index([("created", pymongo.ASCENDING)], expireAfterSeconds=30*24*60*60)

mongo_availability = mongo_db['availability']
mongo_availability.ensure_index([("isbn13", pymongo.ASCENDING)], unique=True)
mongo_availability.ensure_index([("created", pymongo.ASCENDING)], expireAfterSeconds=3600)

mongo_queue = mongo_db['availability_queue']
mongo_queue.ensure_index([("user_id", pymongo.ASCENDING)], unique=True)


def goodreads_session(user_id):
    user = mongo_users.find_one({"user_id":user_id})
    session = OAuth1Session(
        consumer_key = os.environ['GOODREADS_API_KEY'],
        consumer_secret = os.environ['GOODREADS_API_SECRET'],
        access_token = user['access_token'],
        access_token_secret = user['access_token_secret'],
    )

    return session

def goodreads_api():
    goodreads = OAuth1Service(
        consumer_key=os.environ['GOODREADS_API_KEY'],
        consumer_secret=os.environ['GOODREADS_API_SECRET'],
        name='goodreads',
        request_token_url='http://www.goodreads.com/oauth/request_token',
        authorize_url='http://www.goodreads.com/oauth/authorize',
        access_token_url='http://www.goodreads.com/oauth/access_token',
        base_url='http://www.goodreads.com/'
    )

    return goodreads

def goodreads_reading_list(goodreads, user_id):
    isbns = []
    page = 1
    return_count = 1

    while return_count > 0:
        response = goodreads.get('https://www.goodreads.com/review/list', params={
            'format': 'json',
            'v': '2',
            'user': user_id,
            'shelf': 'to-read',
            'page': page
        })

        response_isbns = [book['isbn13'] for book in response.json() if book['isbn13'] is not None]

        isbns = isbns + response_isbns
        return_count = len(response_isbns)
        page += 1

    return isbns

def book_list_details(isbns, goodreads):
    found_books = {}
    for book in mongo_book_details.find({"isbn13":{"$in":isbns}}):
        found_books[book['isbn13']] = book

    for book in mongo_sfpl_books.find({"isbn13":{"$in":found_books.keys()}}):
        found_books[book['isbn13']]['sfpl_books'] = book['entries']

    for book in mongo_availability.find({"isbn13":{"$in":found_books.keys()}}):
        found_books[book['isbn13']]['availability'] = book['availability']

    return [book_details(book, goodreads, found_books) for book in isbns]

def book_details(isbn13, goodreads, cache={}):
    book = cache.get(isbn13, None)

    if not book:
        response = goodreads.get("https://www.goodreads.com/book/isbn", params={'format': 'xml', 'isbn': isbn13})
        tree = ElementTree.fromstring(response.text.encode("utf-8"))
        book = {
            'created': datetime.datetime.utcnow(),
            'title': tree.getchildren()[1].findtext("title"),
            'isbn': tree.getchildren()[1].findtext("isbn"),
            'isbn13': tree.getchildren()[1].findtext("isbn13"),
            'goodreads_id': tree.getchildren()[1].findtext("id"),
            'num_pages': int(tree.getchildren()[1].findtext("num_pages") or '0'),
            'average_rating': float(tree.getchildren()[1].findtext("average_rating") or '0'),
            'author': tree.getchildren()[1].find("authors").getchildren()[0].findtext("name")
        }

        mongo_book_details.insert(book)

    add_sfpl_entries(book)
    add_availability(book)

    return book


def add_sfpl_entries(book):
    if book.get('sfpl_books', None) != None:
        return

    title_string = '+'.join(book['title'].lower().strip().split(' '))
    r = requests.get("http://sflib1.sfpl.org/search~S1/?searchtype=t&searcharg=" + title_string)
    b = BeautifulSoup(r.text)

    entries = b.select(".briefCitRow")
    if len(entries) == 0:
        browse_rows = b.select(".browseEntry")
        if len(browse_rows) > 0:
            newpage = browse_rows[0].find('a', href=re.compile("/search")).attrs['href']
            r = requests.get("http://sflib1.sfpl.org" + newpage)
            b = BeautifulSoup(r.text)
            entries = b.select(".briefCitRow")

    parsed_entries = []
    if len(entries) > 0:
        for entry in entries:
            links = entry.select(".detail a")
            if (len(links) > 0):
                data = {
                    'sfpl_href': 'http://sflib1.sfpl.org' + links[0].attrs['href'],
                    'ebook': len(entry.select('img[alt~=EBOOK]')) > 0
                    #'book': len(sibling.select('img[alt~=BOOK]')) > 0,
                }

                if data['ebook']: #or data['book']:
                    response = requests.get(data['sfpl_href'])
                    page = BeautifulSoup(response.text)

                    # overdrive
                    links = page.find_all(href=re.compile("^http://sfpl.lib.overdrive.com"))
                    if len(links) > 0:
                        data['overdrive_href'] = links[0].attrs['href']

                    # axis 360
                    links = page.find_all(href=re.compile("^http://sfpl.axis360.baker-taylor.com"))
                    if len(links) > 0:
                        data['axis_href'] = links[0].attrs['href']

                    parsed_entries.append(data)


    mongo_sfpl_books.insert({
        'isbn13': book['isbn13'],
        'entries': parsed_entries,
        'created':datetime.datetime.utcnow()
    })

    book['sfpl_books'] = parsed_entries


def add_availability(book):
    if book.get('availability', None) != None:
        return

    availability = []
    for entry in book['sfpl_books']:
        if entry['ebook']:
            availability.append(check_availability(entry))

    mongo_availability.insert({
        'isbn13': book['isbn13'],
        'availability': availability,
        'created':datetime.datetime.utcnow()
    })

    book['availability'] = availability

def check_availability(details):
    if details.get('overdrive_href'):
        details.update(check_overdrive_availability(details['overdrive_href']))
        return details

    if details.get('axis_href'):
        details.update(check_axis_availability(details['axis_href']))
        return details

    return False


def check_axis_availability(link):
    response = requests.get(link)
    page = BeautifulSoup(response.text)
    available = int(page.select("#AvailableQuantity")[0].text.split()[1])
    result = {
        'type': 'axis',
        'service_href': link,
        'available': available > 0,
        'kindle': False,
        'epub': True
    }
    return result

def check_overdrive_availability(link):
    response = requests.get(link)
    page = BeautifulSoup(response.text)
    available = int(page.select(".details-avail-copies span")[0].text)
    formats = '; '.join([f.text for f in page.select("ul.formats-at-download li")])
    result = {
        'type': 'overdrive',
        'service_href': link,
        'available': available > 0,
        'kindle': re.compile("Kindle Book").match(formats) != None,
        'epub': re.compile('Adobe EPUB eBook').match(formats) != None
    }
    return result
