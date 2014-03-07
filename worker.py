import re
import os
import datetime
import pymongo
import requests
import json
import time

from rauth.service import OAuth1Service, OAuth1Session
from elementtree import ElementTree
from bs4 import BeautifulSoup

from util import *

def process_availability_queue():
    while True:
        print("Processing from the queue.")
        for q in mongo_queue.find():
            print(q)
            goodreads = goodreads_session(q['user_id'])
            response = goodreads.get('https://www.goodreads.com/review/list', params={
                'format': 'json',
                'v': '2',
                'user': q['user_id'],
                'shelf': 'to-read',
                'per_page': 200
            })

            isbns = goodreads_reading_list(goodreads, q['user_id'])
            book_list_details(isbns, goodreads)
            mongo_queue.remove({"user_id":q['user_id']})

        time.sleep(5)


print("PROCESS STARTED")
time.sleep(5)
process_availability_queue()
