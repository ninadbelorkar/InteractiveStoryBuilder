import os
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
import bcrypt
from dotenv import load_dotenv
# import google.generativeai as genai
import json
import requests
import sys

# --- App Initialization and Configuration ---
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- Database Connection ---
client = MongoClient(os.getenv("MONGO_DB_URL"))
db = client[os.getenv("DATABASE_NAME")]
users_collection = db["users"]
stories_collection = db["stories"]
nodes_collection = db["story_nodes"]

# --- DYNAMIC AI MODEL CONFIGURATION (THE NEW, ROBUST METHOD) ---
# --- NEW: AI Helper Function (Configure on Demand) ---
# --- NEW: Direct API Call Helper ---
def call_gemini_api(prompt_parts):
    """
    Makes a direct HTTP request to the Gemini API.
    This bypasses the google-generativeai library completely.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("API Key not found in .env file.")
        
    # Using the stable v1 API endpoint directly
    # NOTE: We use gemini-1.5-flash which is fast and excellent for these tasks.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={api_key}"
    
    headers = {
        "Content-Type": "application/json"
    }
    
    data = {
        "contents": [{"parts": [{"text": part} for part in prompt_parts]}]
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status() 
        
        response_json = response.json()
        
        if "candidates" not in response_json or not response_json["candidates"]:
             raise ValueError("AI returned an empty or invalid response.")

        return response_json["candidates"][0]["content"]["parts"][0]["text"]
        
    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP ERROR: {http_err} - Response: {http_err.response.text}", file=sys.stderr)
        raise 
    except Exception as e:
        print(f"DIRECT API CALL ERROR: {e}", file=sys.stderr)
        raise

# --- Main Page-Rendering Routes ---

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/discover")
def discover():
    # This pipeline joins stories with their authors' usernames
    pipeline = [
        {
            "$lookup": {
                "from": "users",
                "localField": "author_id",
                "foreignField": "_id",
                "as": "author_details"
            }
        },
        {
            "$unwind": "$author_details"
        }
    ]
    all_stories = list(stories_collection.aggregate(pipeline))
    return render_template("discover.html", stories=all_stories)

@app.route("/auth", methods=["GET", "POST"])
def auth():
    if request.method == "POST":
        form_type = request.form.get("form_type")
        email = request.form.get("email")
        password = request.form.get("password")

        if form_type == "register":
            username = request.form.get("username")
            if users_collection.find_one({"email": email}):
                flash("Email already exists.", "error")
                return redirect(url_for("auth"))
            
            hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
            users_collection.insert_one({
                "username": username,
                "email": email,
                "password": hashed_password
            })
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("auth"))

        elif form_type == "login":
            user = users_collection.find_one({"email": email})
            if user and bcrypt.checkpw(password.encode('utf-8'), user["password"]):
                session["user_id"] = str(user["_id"])
                session["username"] = user["username"]

                # --- REDIRECT LOGIC ---
                next_url = request.args.get('next')
                if next_url:
                    return redirect(next_url)
                else:
                    return redirect(url_for("dashboard"))

            else:
                flash("Invalid email or password.", "error")
                return redirect(url_for("auth"))
    
    return render_template("auth.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        flash("You must be logged in to view the dashboard.", "error")
        return redirect(url_for("auth"))
    
    user_id = ObjectId(session["user_id"])
    
    # Fetch user's own stories
    user_stories = list(stories_collection.find({"author_id": user_id}))
    
    # --- FETCH PLAYED HISTORY ---
    user = users_collection.find_one({"_id": user_id})
    played_story_ids = user.get("played_history", [])
    
    # Fetch the full story details for the played stories
    # We also join with the author's name
    pipeline = [
        {"$match": {"_id": {"$in": played_story_ids}}},
        {"$lookup": {"from": "users", "localField": "author_id", "foreignField": "_id", "as": "author_details"}},
        {"$unwind": "$author_details"}
    ]
    played_stories = list(stories_collection.aggregate(pipeline))
    # --- END OF FETCH ---

    return render_template("dashboard.html", stories=user_stories, played_stories=played_stories)

@app.route("/editor/<story_id>")
def editor(story_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))
    
    story = stories_collection.find_one({"_id": ObjectId(story_id), "author_id": ObjectId(session["user_id"])})
    if not story:
        flash("Story not found or you don't have permission to edit it.", "error")
        return redirect(url_for("dashboard"))
    
    # Original line:
    # nodes = list(nodes_collection.find({"story_id": ObjectId(story_id)}))
    
    # --- THIS IS THE FIX ---
    # Fetch the nodes from the database
    nodes_from_db = list(nodes_collection.find({"story_id": ObjectId(story_id)}))
    
    # Create a new list where all ObjectId fields are converted to strings
    nodes = []
    for node in nodes_from_db:
        node['_id'] = str(node['_id'])
        node['story_id'] = str(node['story_id'])
        nodes.append(node)
    # --- END OF FIX ---
    
    return render_template("editor.html", story=story, nodes=nodes)

@app.route("/play/<node_id>")
def player(node_id):
    try:
        node = nodes_collection.find_one({"_id": ObjectId(node_id)})
        if not node:
            return "Story node not found.", 404
        
        # --- HISTORY TRACKING LOGIC ---
        if "user_id" in session:
            story = stories_collection.find_one({"_id": ObjectId(node['story_id'])})
            # Check if this node is the STARTING node of the story
            if story and str(node['_id']) == str(story.get('start_node_id')):
                # Add story ID to user's history, but only if it's not already there
                # Using $addToSet prevents duplicates
                users_collection.update_one(
                    {"_id": ObjectId(session['user_id'])},
                    {"$addToSet": {"played_history": ObjectId(story['_id'])}}
                )
        # --- END OF HISTORY TRACKING LOGIC ---

        return render_template("player.html", node=node)
    except Exception:
        return "Invalid story node.", 404
    

@app.route("/api/story/<story_id>/update-details", methods=["POST"])
def api_update_story_details(story_id):
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    stories_collection.update_one(
        {"_id": ObjectId(story_id), "author_id": ObjectId(session["user_id"])},
        {"$set": {
            "title": request.form.get("title"),
            "description": request.form.get("description")
        }}
    )
    # You can flash a message here if you want, but a simple success is fine for an API
    return jsonify({"success": True})


# --- Form Handling and Data Modification Routes ---

@app.route("/story/create", methods=["POST"])
def create_story():
    if "user_id" not in session:
        return redirect(url_for("auth"))
    
    title = request.form.get("title")
    description = request.form.get("description")
    
    story_id = stories_collection.insert_one({
        "title": title,
        "description": description,
        "author_id": ObjectId(session["user_id"]),
        "start_node_id": None,
        "posterImageURL": "",
    }).inserted_id

    first_node_id = nodes_collection.insert_one({
        "story_id": story_id,
        "story_text": "This is the beginning of your story. Edit this page to get started!",
        "backgroundImageURL": "",
        "choices": [],
        "next_node_id": None
    }).inserted_id

    stories_collection.update_one(
        {"_id": story_id},
        {"$set": {"start_node_id": str(first_node_id)}}
    )

    flash("Story created! You are now in the editor.", "success")
    return redirect(url_for("editor", story_id=story_id))

@app.route("/story/delete/<story_id>", methods=["POST"])
def delete_story(story_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    story_object_id = ObjectId(story_id)
    story = stories_collection.find_one({"_id": story_object_id, "author_id": ObjectId(session["user_id"])})

    if story:
        stories_collection.delete_one({"_id": story_object_id})
        nodes_collection.delete_many({"story_id": story_object_id})
        flash("Story deleted successfully.", "success")
    else:
        flash("Could not delete story.", "error")

    return redirect(url_for("dashboard"))


@app.route("/api/story/<story_id>/set-poster", methods=["POST"])
def api_set_poster(story_id):
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    image_url = request.json.get("posterImageURL")
    
    stories_collection.update_one(
        {"_id": ObjectId(story_id), "author_id": ObjectId(session["user_id"])},
        {"$set": {"posterImageURL": image_url}}
    )
    return jsonify({"success": True, "message": "Poster updated!"})


# --- API Endpoints (for JavaScript interaction in the editor) ---

@app.route("/api/node/<node_id>", methods=["GET", "POST"])
def api_node(node_id):
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "GET":
        node = nodes_collection.find_one({"_id": ObjectId(node_id)})
        if not node:
            return jsonify({"error": "Node not found"}), 404
        node["_id"] = str(node["_id"])
        node["story_id"] = str(node["story_id"])
        return jsonify(node)

    if request.method == "POST":
        data = request.json
        nodes_collection.update_one(
            {"_id": ObjectId(node_id)},
            {"$set": {
                "story_text": data["storyText"],
                "choices": data["choices"],
                "backgroundImageURL": data.get("backgroundImageURL"),
                "next_node_id": data.get("nextNodeId")
            }}
        )
        return jsonify({"success": True, "message": "Node saved!"})

@app.route("/api/story/<story_id>/nodes", methods=["POST"])
def api_create_node(story_id):
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    new_node_id = nodes_collection.insert_one({
        "story_id": ObjectId(story_id),
        "story_text": "New Page",
        "backgroundImageURL": "",
        "choices": [],
        "next_node_id": None
    }).inserted_id
    
    return jsonify({"success": True, "newNodeId": str(new_node_id)})


@app.route('/api/ai/generate-ending', methods=['POST'])
def ai_generate_ending():
    story_id = request.json.get('story_id')
    story = stories_collection.find_one({"_id": ObjectId(story_id)})
    story_title = story.get('title', 'this story')
    prompt = [f'A reader finished a story titled "{story_title}". Write a short, satisfying, concluding thought.']
    try:
        response_text = call_gemini_api(prompt)
        return jsonify({'ending_text': response_text.strip()})
    except Exception:
        return jsonify({'ending_text': 'Every story has an end, and this is yours. Thank you for the journey.'})


# --- AI API Endpoints ---

@app.route('/api/ai/enrich', methods=['POST'])
def ai_enrich():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    prompt_text = request.json.get('prompt')
    if not prompt_text: return jsonify({'error': 'Prompt is required'}), 400
    full_prompt = ["You are a creative co-author...", f"Scene Idea: {prompt_text}"]
    try:
        response_text = call_gemini_api(full_prompt)
        return jsonify({'enriched_text': response_text.strip()})
    except Exception as e:
        return jsonify({'error': f"AI service error: {e}"}), 500
    

@app.route('/api/ai/suggest-choices', methods=['POST'])
def ai_suggest_choices():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
        
    description = request.json.get('description')
    story_title = request.json.get('story_title')
    if not description: 
        return jsonify({'error': 'Scene description is required'}), 400

    full_prompt = [
        f"You are a game designer for an interactive story titled '{story_title}'.",
        "Based on the following scene, suggest three creative, distinct, and compelling choices for the player to make.",
        "The choices should move the plot forward in interesting ways. Do not use generic actions like 'look around' or 'wait'.",
        "Return the response as a simple list of strings, separated by newlines. Do not add any other text like titles or bullet points.",
        f"Scene: {description}"
    ]
    
    # --- THIS IS THE MISSING PART ---
    try:
        response_text = call_gemini_api(full_prompt)
        choices = [line.strip().lstrip('-* ') for line in response_text.strip().split('\n')]
        return jsonify({'choices': choices})
    except Exception as e:
        return jsonify({'error': f"AI service error: {e}"}), 500
    


if __name__ == "__main__":
    app.run(debug=True)