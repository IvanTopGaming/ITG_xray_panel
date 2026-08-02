from panel_core.bootstrap import bootstrap_gevent

bootstrap_gevent()

from panel_core.dispatch import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
