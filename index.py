import re
import os
import datetime
import pymongo
import requests
import json

from flask import Flask
from flask import abort, redirect, url_for, session, request, render_template

from rauth.service import OAuth1Service, OAuth1Session
from elementtree import ElementTree
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = os.environ['COOKIE_SECRET']

mongo_client = pymongo.MongoClient(os.environ['MONGOHQ_URL'])
mongo_db = mongo_client['reading-list']

mongo_users = mongo_db['users']
mongo_users.ensure_index([("user_id", pymongo.ASCENDING)], unique=True)

mongo_pending_oauth = mongo_db['pending_oauth']
mongo_pending_oauth.ensure_index([("created", pymongo.ASCENDING)], expireAfterSeconds=3600)

mongo_book_details = mongo_db['books']
mongo_users.ensure_index([("isbn13", pymongo.ASCENDING)], unique=True)

mongo_availability = mongo_db['availability']
mongo_availability.ensure_index([("created", pymongo.ASCENDING)], expireAfterSeconds=3600)


@app.route('/')
def index():
    if session.get('user_id', None):
        goodreads = goodreads_session(session['user_id'])
        response = goodreads.get('https://www.goodreads.com/review/list', params={
            'format': 'json',
            'v': '2',
            'user': session['user_id'],
            'shelf': 'to-read'
        })

        details = book_list_details([book['isbn13'] for book in response.json()], goodreads)
        return render_template('index.html', user_id=session['user_id'], book_details=details)
    else:
        return redirect(url_for('login'))

@app.route('/login')
def login():
    goodreads = goodreads_api()
    request_token, request_token_secret = goodreads.get_request_token(header_auth=True)
    authorize_url = goodreads.get_authorize_url(request_token, oauth_callback="http://"+os.environ['HOST']+"/callback")
    mongo_pending_oauth.insert({"token": request_token, "secret":request_token_secret, "created":datetime.datetime.utcnow()})

    return render_template('login.html', authorize_url=authorize_url)

@app.route('/callback')
def oauth_callback():
    token = request.args.get('oauth_token', None)
    authorized = request.args.get('authorize', None)

    if authorized == '1':
        details = mongo_pending_oauth.find_one({"token": token})
        goodreads = goodreads_api()
        goodreads_session = goodreads.get_auth_session(token, details['secret'])
        access_token = goodreads_session.access_token
        access_token_secret = goodreads_session.access_token_secret

        response = goodreads_session.get('http://www.goodreads.com/api/auth_user', params={'format':'json'})
        tree = ElementTree.fromstring(response.text.encode("utf-8"))
        user_id = tree.find('user').attrib['id']

        user = mongo_users.find_one({"user_id":user_id}) or {"user_id":user_id, "created":datetime.datetime.utcnow()}
        user['access_token'] = access_token
        user['access_token_secret'] = access_token_secret
        mongo_users.save(user)

        session['user_id'] = user_id

        return redirect(url_for('index'))
    else:
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    # remove the username from the session if it's there
    session.pop('user_id', None)
    return redirect(url_for('login'))


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

def book_list_details(isbns, goodreads):
    found_books = {}
    for book in mongo_book_details.find({"isbn13":{"$in":isbns}}):
        found_books[book['isbn13']] = book

    for availability in mongo_availability.find({"isbn13":{"$in":found_books.keys()}}):
        found_books[availability['isbn13']]['availability'] = availability['availability']

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

    add_availability(book)

    return book

def add_availability(book):
    if book.get('availability', None) != None:
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
                    parsed_entries.append(check_availability(data))

    mongo_availability.insert({
        'isbn13': book['isbn13'],
        'availability': parsed_entries,
        'created':datetime.datetime.utcnow()
    })

    book['availability'] = parsed_entries

def check_availability(details):
    response = requests.get(details['sfpl_href'])
    page = BeautifulSoup(response.text)

    # overdrive
    links = page.find_all(href=re.compile("^http://sfpl.lib.overdrive.com"))
    if len(links) > 0:
        details.update(check_overdrive_availability(links[0].attrs['href']))
        return details

    # axis 360
    links = page.find_all(href=re.compile("^http://sfpl.axis360.baker-taylor.com"))
    if len(links) > 0:
        details.update(check_axis_availability(links[0].attrs['href']))
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
    formats = [f.text for f in page.select("ul.formats-at-download li")]
    result = {
        'type': 'overdrive',
        'service_href': link,
        'available': available > 0,
        'kindle': 'Kindle Book' in formats,
        'epub': 'Adobe EPUB eBook' in formats
    }
    return result



if __name__ == '__main__':
    app.debug = os.environ['HOST'] == "127.0.0.1:5000"
    app.run()

