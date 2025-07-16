# api/try.py — Vercel Serverless Function for LeetCode → Firebase

import requests
import json
import os
import datetime

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase Admin
if not firebase_admin._apps:
    service_account_info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_KEY"])
    cred = credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# -------------------- LeetCode Query Helpers --------------------

BASE_URL = "https://leetcode.com/graphql"

def run_query(query, variables=None):
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com",
    }
    response = requests.post(BASE_URL, json={"query": query, "variables": variables}, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        print("Error:", response.status_code, response.text)
        return {}

def get_name(username):
    query = """
    query userPublicProfile($username: String!) {
      matchedUser(username: $username) {
        username
        profile {
          realName
        }
      }
    }
    """
    data = run_query(query, {"username": username})
    matched_user = data.get("data", {}).get("matchedUser", {})
    if not matched_user:
        return None
    return matched_user["profile"]["realName"] or matched_user["username"]

def get_problems_solved(username):
    query = """
    query userProblemsSolved($username: String!) {
      matchedUser(username: $username) {
        submitStatsGlobal {
          acSubmissionNum {
            difficulty
            count
          }
        }
      }
    }
    """
    data = run_query(query, {"username": username})
    stats = data.get("data", {}).get("matchedUser", {}).get("submitStatsGlobal", {}).get("acSubmissionNum", [])
    return {entry["difficulty"]: entry["count"] for entry in stats}

def get_last_submission(username):
    query = """
    query recentAcSubmissions($username: String!, $limit: Int!) {
      recentAcSubmissionList(username: $username, limit: $limit) {
        title
        titleSlug
        timestamp
        lang
      }
    }
    """
    data = run_query(query, {"username": username, "limit": 1})
    subs = data.get("data", {}).get("recentAcSubmissionList", [])
    if not subs:
        return None
    sub = subs[0]
    return {
        "title": sub["title"],
        "lang": sub["lang"],
        "url": f"https://leetcode.com/problems/{sub['titleSlug']}/",
        "timestamp": datetime.datetime.fromtimestamp(int(sub["timestamp"])).isoformat()
    }

def get_leetcode_summary(username):
    name = get_name(username)
    solved = get_problems_solved(username)
    last = get_last_submission(username)

    return {
        "name": name,
        "username": username,
        "problems_solved": solved,
        "last_submission": last or "No recent submissions found"
    }

# -------------------- Vercel Handler --------------------

def handler(request, response):
    try:
        username_to_scrape = request.query.get("username", "sinha_i_prefer")

        if not username_to_scrape:
            return response.status(400).json({"error": "Missing 'username' query parameter."})

        leetcode_data = get_leetcode_summary(username_to_scrape)

        if not leetcode_data["name"]:
            return response.status(404).json({"error": f"LeetCode user '{username_to_scrape}' not found."})

        # Save to Firestore
        doc_ref = db.collection("leetcodeUsers").document(username_to_scrape)
        leetcode_data["last_updated"] = firestore.SERVER_TIMESTAMP
        doc_ref.set(leetcode_data)

        return response.status(200).json({
            "message": f"LeetCode data for '{username_to_scrape}' saved to Firestore.",
            "data": leetcode_data,
            "firestoreDocPath": f"leetcodeUsers/{username_to_scrape}"
        })

    except Exception as e:
        return response.status(500).json({"error": f"Unexpected error: {str(e)}"})
