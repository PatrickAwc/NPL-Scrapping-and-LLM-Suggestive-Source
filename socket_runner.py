import eventlet
eventlet.monkey_patch()

from app import app
from llm_src.chatSocket import init_socketio


if __name__ == '__main__':
    # Waitress running port: 5001
    # Socket io running port : 5002
    socketio = init_socketio(app)
    socketio.run(app, host='0.0.0.0', port=5002)