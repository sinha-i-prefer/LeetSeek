# -*- coding: utf-8 -*-
import requests
import json
import datetime
import firebase_admin
from firebase_admin import credentials, firestore, messaging
import os
import base64
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# --- Firebase Initialization ---
# This section securely initializes Firebase using an environment variable.
try:
    # Get the Base64 encoded service account from environment variables
    base64_creds = os.environ.get('FIREBASE_SERVICE_ACCOUNT_BASE64')
    if not base64_creds:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_BASE64 environment variable not set.")

    # Decode the Base64 string to a JSON string
    decoded_creds = base64.b64decode(base64_creds).decode('utf-8')
    service_account_info = json.loads(decoded_creds)

    # Initialize the app if not already initialized
    if not firebase_admin._apps:
        cred = credentials.Certificate(service_account_info)
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    FIREBASE_INITIALIZED = True
except Exception as e:
    db = None
    FIREBASE_INITIALIZED = False
    FIREBASE_ERROR = str(e)


# --- LeetCode API Logic ---
BASE_URL = "https://leetcode.com/graphql"

def run_query(query, variables=None):
    """Sends a query to the LeetCode GraphQL API."""
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com",
    }
    try:
        response = requests.post(BASE_URL, json={"query": query, "variables": variables}, headers=headers, timeout=10)
        response.raise_for_status()  # Raise an exception for bad status codes
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to LeetCode API: {e}")
        return {}

def get_leetcode_summary(username):
    """Fetches a complete summary for a given LeetCode username."""
    
    # GraphQL query to get user profile, stats, and last submission in one request
    query = """
    query userPublicProfileAndRecentSubs($username: String!) {
      matchedUser(username: $username) {
        username
        profile {
          realName
        }
        submitStatsGlobal {
          acSubmissionNum {
            difficulty
            count
          }
        }
      }
      recentAcSubmissionList(username: $username, limit: 1) {
        title
        titleSlug
        timestamp
        lang
      }
    }
    """
    
    data = run_query(query, {"username": username})
    
    # Handle API response errors or missing data
    if not data.get("data"):
        return {"error": "Failed to fetch data from LeetCode API."}
        
    matched_user = data["data"].get("matchedUser")
    if not matched_user:
        return {"error": f"User '{username}' not found on LeetCode."}

    # Process user profile
    name = matched_user.get("profile", {}).get("realName") or matched_user.get("username")

    # Process solved problems
    stats = matched_user.get("submitStatsGlobal", {}).get("acSubmissionNum", [])
    solved = {entry["difficulty"]: entry["count"] for entry in stats}

    # Process last submission
    last_submission_data = data["data"].get("recentAcSubmissionList", [])
    last_submission = None
    if last_submission_data:
        sub = last_submission_data[0]
        last_submission = {
            "title": sub["title"],
            "lang": sub["lang"],
            "url": f"https://leetcode.com/problems/{sub['titleSlug']}/",
            "timestamp": datetime.datetime.fromtimestamp(int(sub["timestamp"])).isoformat()
        }

    return {
        "name": name,
        "username": username,
        "problems_solved": solved,
        "last_submission": last_submission
    }


# --- FCM Notification Logic ---
def notify_friends_of_activity(username, name, last_submission):
    """
    When a user has new activity, find all users who have them as a friend
    and send FCM push notifications to their devices.
    """
    if not last_submission:
        return 0

    try:
        # Collection group query: find all friend docs where username matches
        friends_query = db.collection_group("friends").where("username", "==", username).stream()

        notifications_sent = 0
        for friend_doc in friends_query:
            # The parent path is: users/{uid}/friends/{friendDoc}
            # Navigate up to get the uid
            parent_ref = friend_doc.reference.parent.parent
            if not parent_ref:
                continue

            uid = parent_ref.id

            # Get this user's FCM token
            try:
                user_doc = db.collection("users").document(uid).get()
                if not user_doc.exists:
                    continue

                fcm_token = user_doc.to_dict().get("fcmToken")
                if not fcm_token:
                    continue

                # Build and send the notification
                message = messaging.Message(
                    data={
                        "friendName": name or username,
                        "problemTitle": last_submission.get("title", "a new problem"),
                        "problemLang": last_submission.get("lang", ""),
                    },
                    notification=messaging.Notification(
                        title=f"🔥 {name or username} solved a problem!",
                        body=f"{last_submission.get('title', 'New problem')} ({last_submission.get('lang', '')})",
                    ),
                    token=fcm_token,
                )

                messaging.send(message)
                notifications_sent += 1
                print(f"FCM: Notified {uid} about {username}'s activity")

            except Exception as e:
                print(f"FCM: Failed to notify {uid}: {e}")

        return notifications_sent

    except Exception as e:
        print(f"FCM: Error in notify_friends_of_activity: {e}")
        return 0


# --- Vercel Serverless Handler ---
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handles GET requests for single users or scheduled cron jobs."""
        query_components = parse_qs(urlparse(self.path).query)
        username = query_components.get('username', [None])[0]
        # Check for a specific query parameter to identify the cron job
        source = query_components.get('source', [None])[0]

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        
        response = {}

        # --- Firebase Initialization Check ---
        if not FIREBASE_INITIALIZED:
            response = {"status": "error", "message": "Firebase initialization failed.", "details": FIREBASE_ERROR}
            self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))
            return

        # --- Cron Job Logic ---
        if source == 'cron':
            try:
                # 1. Get all existing usernames from Firestore
                users_ref = db.collection("leetcodeUsers").stream()
                
                # Build a dict of old data so we can detect changes
                old_data = {}
                usernames = []
                for doc in users_ref:
                    uname = doc.id
                    usernames.append(uname)
                    doc_dict = doc.to_dict()
                    old_submission = doc_dict.get("last_submission")
                    old_data[uname] = {
                        "last_submission": old_submission
                    }
                
                updated_count = 0
                failed_users = []
                total_notifications = 0

                # 2. Loop through each username and update their data
                for uname in usernames:
                    try:
                        print(f"CRON: Updating {uname}...")
                        leetcode_data = get_leetcode_summary(uname)
                        if "error" not in leetcode_data:
                            leetcode_data["last_updated"] = firestore.SERVER_TIMESTAMP
                            db.collection("leetcodeUsers").document(uname).set(leetcode_data, merge=True)
                            updated_count += 1

                            # Check if last_submission changed → notify friends
                            new_submission = leetcode_data.get("last_submission")
                            old_submission = old_data.get(uname, {}).get("last_submission")

                            if new_submission and new_submission != old_submission:
                                sent = notify_friends_of_activity(
                                    username=uname,
                                    name=leetcode_data.get("name", uname),
                                    last_submission=new_submission
                                )
                                total_notifications += sent
                                print(f"CRON: {uname} has new activity, sent {sent} notifications")
                        else:
                            failed_users.append(uname)
                    except Exception as e:
                        print(f"CRON: Failed to update {uname}: {e}")
                        failed_users.append(uname)
                
                response = {
                    "status": "success",
                    "job": "cron_update_all",
                    "total_users_found": len(usernames),
                    "updated_successfully": updated_count,
                    "failed_to_update": len(failed_users),
                    "failed_users": failed_users,
                    "notifications_sent": total_notifications
                }
            except Exception as e:
                response = {"status": "error", "job": "cron_update_all", "details": str(e)}

        # --- Single User Logic (CORRECTED) ---
        elif username:
            try:
                leetcode_data = get_leetcode_summary(username)
                
                # Check if the data contains an error (invalid user)
                if "error" in leetcode_data:
                    # If error, return failure response and DO NOT write to DB
                    response = {"status": "error", "message": leetcode_data["error"]}
                else:
                    # Only write to DB if the user is valid
                    leetcode_data["last_updated"] = firestore.SERVER_TIMESTAMP
                    doc_ref = db.collection("leetcodeUsers").document(username)
                    doc_ref.set(leetcode_data)
                    
                    # Remove non-serializable field for the JSON response
                    if "last_updated" in leetcode_data:
                        del leetcode_data["last_updated"]
                        
                    response = {
                        "status": "success",
                        "message": f"Successfully fetched and stored data for {username}.",
                        "data": leetcode_data
                    }
            except Exception as e:
                response = {"status": "error", "message": "An internal error occurred.", "details": str(e)}
        
        # --- No Valid Parameter Logic ---
        else:
            response = {"status": "error", "message": "Please provide a 'username' query parameter or use '?source=cron'."}

        self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))
        return