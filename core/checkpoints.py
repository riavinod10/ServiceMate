import os

def get_checkpointer():
    if not os.environ.get("POSTGRES_DB"):
        return None
    from langgraph.checkpoint.postgres import PostgresSaver
    connection = "postgresql://{0}:{1}@{2}:{3}/{4}".format(os.environ.get("POSTGRES_USER", "servicemate"), os.environ.get("POSTGRES_PASSWORD", "servicemate"), os.environ.get("POSTGRES_HOST", "localhost"), os.environ.get("POSTGRES_PORT", "5432"), os.environ["POSTGRES_DB"])
    return PostgresSaver.from_conn_string(connection)
