from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.secret_key = "your_secret_key"

app.config['SQLALCHEMY_DATABASE_URI'] = "sqlite:///users.db"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)
socketio = SocketIO(app, manage_session=False)

# --- Database Models ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(50), nullable=False, unique=True)
    username = db.Column(db.String(20), nullable=False, unique=True)
    password = db.Column(db.String(200), nullable=False)

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, nullable=False)


class FriendRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False)
    accepted = db.Column(db.Boolean, default=False)


# --- Global online users mapping ---
online_users = {}
connected_users = {}

# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid username or password", "danger")
    return render_template('login.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        username = request.form.get('username')
        password = request.form.get('password')

        existing_user = User.query.filter((User.email == email) | (User.username == username)).first()
        if existing_user:
            flash("Email or Username already registered!", "danger")
            return redirect(url_for('signup'))

        hashed_password = generate_password_hash(password)
        user = User(name=name, email=email, username=username, password=hashed_password)
        db.session.add(user)
        db.session.commit()
        flash("Account created successfully! Please login.", "success")
        return redirect(url_for('login'))
    return render_template('signup.html')


@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully!", "info")
    return redirect(url_for('login'))


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    # Pending friend requests
    requests = FriendRequest.query.filter_by(receiver_id=user_id, accepted=False).all()
    request_senders = [User.query.get(r.sender_id) for r in requests]

    # Friends list
    friends = FriendRequest.query.filter(
        ((FriendRequest.sender_id == user_id) | (FriendRequest.receiver_id == user_id)) &
        (FriendRequest.accepted == True)
    ).all()
    friend_ids = [f.sender_id if f.sender_id != user_id else f.receiver_id for f in friends]
    friends_list = User.query.filter(User.id.in_(friend_ids)).all()

    # Already sent requests
    sent_requests = FriendRequest.query.filter_by(sender_id=user_id).all()
    sent_ids = [r.receiver_id for r in sent_requests]

    # All users excluding self, friends, and sent/pending requests
    excluded_ids = friend_ids + sent_ids + [user_id]
    all_users = User.query.filter(~User.id.in_(excluded_ids)).all()

    return render_template("dashboard.html",
                           users=all_users,
                           friends=friends_list,
                           requests=request_senders,
                           user_id=user_id,
                           username=session['username'],
                           sent_ids=sent_ids)


@app.route('/send_request/<int:receiver_id>', methods=['POST'])
def send_request(receiver_id):
    user_id = session['user_id']
    existing = FriendRequest.query.filter_by(sender_id=user_id, receiver_id=receiver_id).first()
    if not existing:
        req = FriendRequest(sender_id=user_id, receiver_id=receiver_id)
        db.session.add(req)
        db.session.commit()
    return redirect(url_for('dashboard'))


@app.route('/accept_request/<int:sender_id>', methods=['POST'])
def accept_request(sender_id):
    user_id = session['user_id']
    req = FriendRequest.query.filter_by(sender_id=sender_id, receiver_id=user_id, accepted=False).first()
    if req:
        req.accepted = True
        db.session.commit()
    return redirect(url_for('dashboard'))


@app.route('/chatroom/<int:friend_id>')
def chatroom(friend_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    friend = User.query.get(friend_id)

    return render_template("chatroom.html",
                           friend=friend,
                           user_id=user_id,
                           username=session['username'])


@app.route('/messages/<int:friend_id>')
def get_messages(friend_id):
    user_id = session['user_id']
    messages = Message.query.filter(
        ((Message.sender_id == user_id) & (Message.receiver_id == friend_id)) |
        ((Message.sender_id == friend_id) & (Message.receiver_id == user_id))
    ).all()
    messages_data = [{'sender_id': m.sender_id, 'receiver_id': m.receiver_id, 'content': m.content} for m in messages]
    return jsonify(messages_data)


# --- SocketIO Events ---
@socketio.on('connect')
def handle_connect():
    user_id = session.get('user_id')
    username = session.get('username')
    if user_id:
        online_users[user_id] = username
        connected_users[user_id] = request.sid
        emit('update_users', online_users, broadcast=True)


@socketio.on('disconnect')
def handle_disconnect():
    user_id = session.get('user_id')
    if user_id in online_users:
        del online_users[user_id]
    if user_id in connected_users:
        del connected_users[user_id]
    emit('update_users', online_users, broadcast=True)


@socketio.on('join')
def handle_join(data):
    room = data['room']
    join_room(room)


@socketio.on('send_message')
def handle_message(data):
    room = data['room']
    msg = data['msg']
    sender_id = int(data['sender_id'])
    receiver_id = int(data['receiver_id'])

    message = Message(sender_id=sender_id, receiver_id=receiver_id, content=msg)
    db.session.add(message)
    db.session.commit()

    emit('receive_message', {'msg': msg, 'sender_id': sender_id}, room=room)


@socketio.on('typing')
def handle_typing(data):
    room = data['room']
    emit('user_typing', {'username': data['username'], 'room': room}, room=room, include_self=False)


@socketio.on('stop_typing')
def handle_stop_typing(data):
    room = data['room']
    emit('stop_typing', {'room': room}, room=room, include_self=False)


# --- Main ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True)
