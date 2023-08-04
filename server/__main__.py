import multiprocessing

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")

    from .app import app, socketio_app
    from . import CONFIG
    
    socketio_app.run(
        app, host="0.0.0.0", port=CONFIG["PORT"], debug=True, use_reloader=False
    )
