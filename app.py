from flask import Flask, render_template, send_from_directory, request
from flask_socketio import SocketIO
from pydub import AudioSegment
import time, os, tempfile, pickle, atexit, uuid

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

STATE_FILE = "state.pkl"

current_song = {
    "title": None,
    "start_time": None,
    "duration": None,
    "display_title": None
}

connected_users = set()
active_listeners = set()
song_queue = []
MAX_QUEUE_LENGTH = 10
original_filenames = {}

def match_target_amplitude(sound, target_dBFS):
    change_in_dBFS = target_dBFS - sound.dBFS
    return sound.apply_gain(change_in_dBFS)

def broadcast_user_count():
    socketio.emit("user_count", len(connected_users))
    socketio.emit("heard_count", len(active_listeners))

def broadcast_queue():
    queue_titles = [original_filenames.get(title, title.split(".")[0]) for title in song_queue]
    socketio.emit("queue_update", queue_titles)

def play_next():
    if song_queue:
        next_song = song_queue.pop(0)
        file_path = os.path.join("static", next_song)
        if os.path.exists(file_path):
            audio = AudioSegment.from_file(file_path)
            current_song["title"] = next_song
            current_song["display_title"] = original_filenames.get(next_song, next_song.split(".")[0])
            current_song["start_time"] = time.time()
            current_song["duration"] = audio.duration_seconds
            socketio.emit("sync", current_song)
            socketio.emit("resume", to="*")
            broadcast_queue()

@app.route("/")
def index():
    socketio.emit("play_next")
    return render_template("index.html")

@app.route("/upload")
def upload():
    return render_template("upload.html")

@app.route('/upload_success', methods=['POST'])
def upload_success():
    files = request.files.getlist('file[]')
    if not files or all(f.filename == '' for f in files):
        return "No files selected", 400

    allowed_extensions = {"mp3", "wav", "ogg", "flac", "m4a", "opus"}

    for f in files:
        if f and f.filename != '':
            ext = f.filename.rsplit(".", 1)[-1].lower()
            if ext not in allowed_extensions:
                continue

            temp_dir = tempfile.mkdtemp()
            original_path = os.path.join(temp_dir, f.filename)
            f.save(original_path)

            try:
                audio = AudioSegment.from_file(original_path)
                audio = match_target_amplitude(audio, -20.0)
                base_name = os.path.splitext(f.filename)[0]
                unique_name = f"{uuid.uuid4().hex}.mp3"
                output_path = os.path.join("static", unique_name)
                audio.export(output_path, format="mp3")
                if len(song_queue) < MAX_QUEUE_LENGTH:
                    song_queue.append(unique_name)
                    original_filenames[unique_name] = base_name
            except Exception as e:
                print(f"Error processing {f.filename}: {e}")

    if current_song["start_time"] is None:
        play_next()

    return render_template("success.html", name="Uploaded!")

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)

@socketio.on("connect")
def on_connect():
    sid = request.sid
    connected_users.add(sid)
    socketio.emit("sync", current_song, to=sid)
    broadcast_user_count()
    broadcast_queue()

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    connected_users.discard(sid)
    active_listeners.discard(sid)
    broadcast_user_count()

@socketio.on("enqueue")
def enqueue_song(data):
    title = data.get("title")
    if title and len(song_queue) < MAX_QUEUE_LENGTH:
        song_queue.append(title)
        if current_song["start_time"] is None:
            play_next()
        broadcast_queue()

@socketio.on("pause_song")
def pause_song():
    socketio.emit("pause", to=request.sid)

@socketio.on("resume_song")
def resume_song():
    sid = request.sid
    active_listeners.add(sid)
    socketio.emit("resume", to=sid)
    broadcast_user_count()

@socketio.on("heard_check")
def heard_check(data):
    sid = request.sid
    if data.get("heard"):
        active_listeners.add(sid)
    else:
        active_listeners.discard(sid)
    broadcast_user_count()

@socketio.on("play_next")
def handle_play_next():
    if current_song["start_time"] is None:
        if song_queue:
            play_next()
        else:
            socketio.emit("announcement", "The queue is empty.")
    else:
        socketio.emit("sync", current_song, to=request.sid)

@socketio.on("song_ended")
def on_song_ended():
    if current_song["start_time"] is not None:
        elapsed = time.time() - current_song["start_time"]
        if elapsed >= current_song["duration"] - 1:
            current_song["start_time"] = None
            current_song["title"] = None
            current_song["display_title"] = None
            current_song["duration"] = None
            play_next()


def save_state():
    with open(STATE_FILE, "wb") as f:
        pickle.dump({
            "queue": song_queue,
            "current_song": current_song,
            "filenames": original_filenames
        }, f)

def load_state():
    global song_queue, current_song, original_filenames
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "rb") as f:
            state = pickle.load(f)
            song_queue = state.get("queue", [])
            current_song = state.get("current_song", current_song)
            original_filenames = state.get("filenames", {})

atexit.register(save_state)
load_state()

if __name__ == "__main__":
    if not os.path.exists("static"):
        os.makedirs("static")
    try:
        socketio.run(app, host="0.0.0.0", port=8000, debug=True)
    except KeyboardInterrupt:
        print("Server stopped by user.")
