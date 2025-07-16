# api/scrape.py
import requests
from bs4 import BeautifulSoup # BeautifulSoup might not be strictly needed for LeetCode GraphQL, but included for general scraping
import json
import os # For environment variables
import datetime # For timestamp conversion
import base64

# Import Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

# Initialize Firebase Admin SDK globally (or within handler if needed, but global is fine for Vercel)
# Ensure this is done only once.
if not firebase_admin._apps:
    # Get service account key from environment variable
    # Vercel will inject this as a string
    encoded_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT_B64")
    decoded_bytes = base64.b64decode(encoded_key)
    service_account_info = json.loads(decoded_bytes)
    cred = credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# --- Your LeetCode Data Collector Functions ---
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
# --- End of Your LeetCode Data Collector Functions ---


def handler(request, response):
    """
    Vercel Serverless Function handler for LeetCode data scraping.
    This function is triggered by an HTTP request.
    """
    try:
        # Get username from query parameter. Default to a sample username if not provided.
        # You might want to make this mandatory or fetch a list of usernames to scrape.
        username_to_scrape = request.query.get('username', 'sinha_i_prefer') # Replace with a default or error if missing

        if not username_to_scrape:
            response.status(400).json({"error": "Missing 'username' query parameter."})
            return

        # --- Call your LeetCode scraping logic ---
        leetcode_data = get_leetcode_summary(username_to_scrape)

        if not leetcode_data["name"]: # Check if user was found
            response.status(404).json({"error": f"LeetCode user '{username_to_scrape}' not found or data unavailable."})
            return

        # --- Store Data in Firestore ---
        # Create a document for each user, or update an existing one
        # Using the username as the document ID makes it easy to retrieve specific user data
        doc_ref = db.collection("leetcodeUsers").document(username_to_scrape)

        # Add a timestamp for when this data was last updated
        leetcode_data["last_updated"] = firestore.SERVER_TIMESTAMP

        # Set (create or overwrite) the document
        doc_ref.set(leetcode_data)

        print(f"LeetCode data for '{username_to_scrape}' saved/updated in Firestore.")

        # --- Return a JSON Response ---
        response.status(200).json({
            "message": f"LeetCode data for '{username_to_scrape}' scraped and saved to Firestore!",
            "data": leetcode_data,
            "firestoreDocPath": f"leetcodeUsers/{username_to_scrape}"
        })

    except requests.exceptions.RequestException as e:
        response.status(500).json({"error": f"HTTP Request failed during LeetCode API call: {e}"})
    except Exception as e:
        response.status(500).json({"error": f"An unexpected error occurred: {e}"})
