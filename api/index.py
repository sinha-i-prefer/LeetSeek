# -*- coding: utf-8 -*-
import requests
import json
import datetime
import firebase_admin
from firebase_admin import credentials, firestore
import os
import base64
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# --- Firebase Initialization ---
# IMPORTANT: This section securely initializes Firebase using an environment variable.
# It decodes the Base64 service account key stored in Vercel's settings.
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
        "last_submission": last_submission or "No recent AC submissions found."
    }

# --- Vercel Serverless Handler ---
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handles GET requests to the serverless function."""
        # Parse username from query parameters (e.g., /api?username=test)
        query_components = parse_qs(urlparse(self.path).query)
        username = query_components.get('username', [None])[0]

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()

        response = {}

        # Check if Firebase is initialized
        if not FIREBASE_INITIALIZED:
            response = {"status": "error", "message": "Firebase initialization failed.", "details": FIREBASE_ERROR}
            self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))
            return

        # Check if username is provided
        if not username:
            response = {"status": "error", "message": "Please provide a 'username' query parameter."}
            self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))
            return

        try:
            # Fetch data from LeetCode
            leetcode_data = get_leetcode_summary(username)

            if "error" in leetcode_data:
                response = {"status": "error", "message": leetcode_data["error"]}
            else:
                # Add server timestamp and write to Firestore
                leetcode_data["last_updated"] = firestore.SERVER_TIMESTAMP
                doc_ref = db.collection("leetcodeUsers").document(username)
                doc_ref.set(leetcode_data)
                
                response = {
                    "status": "success",
                    "message": f"Successfully fetched and stored data for {username}.",
                    "firestore_path": f"leetcodeUsers/{username}",
                    "data": leetcode_data
                }
                # Note: SERVER_TIMESTAMP won't be JSON serializable, so we remove it for the response
                del response["data"]["last_updated"]

        except Exception as e:
            response = {"status": "error", "message": "An internal error occurred.", "details": str(e)}

        self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))
        return
                
