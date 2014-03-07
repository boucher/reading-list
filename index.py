import re
import os
import datetime
import pymongo
import requests
import json
import thread
import time

from flask import Flask
from flask import abort, redirect, url_for, session, request, render_template

from rauth.service import OAuth1Service, OAuth1Session
from elementtree import ElementTree
from bs4 import BeautifulSoup

from util import *

app = Flask(__name__)
app.secret_key = os.environ['COOKIE_SECRET']


@app.route('/')
def index():
    if session.get('user_id', None):
        goodreads = goodreads_session(session['user_id'])
        isbns = goodreads_reading_list(goodreads, session['user_id'])
        count = mongo_availability.find({"isbn13":{"$in":isbns}})

        if count.count() == len(isbns):
            details = book_list_details(isbns, goodreads)
            return render_template('index.html', user_id=session['user_id'], book_details=details)
        else:
            if not mongo_queue.find_one({"user_id":session['user_id']}):
                mongo_queue.insert({"user_id":session['user_id']})
            return render_template('loading.html')
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

@app.route("/sheena")
def sheena():
    session['user_id'] = "27557103"
    return redirect(url_for("index"))


@app.route("/ross")
def ross():
    session['user_id'] = "21000568"
    return redirect(url_for("index"))


@app.route('/logout')
def logout():
    # remove the username from the session if it's there
    session.pop('user_id', None)
    return redirect(url_for('login'))



print("SERVER STARTED")
if __name__ == '__main__':
    app.debug = os.environ['HOST'] == "127.0.0.1:5000"
    app.run()

