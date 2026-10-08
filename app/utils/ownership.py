"""Exclusive OS-backed ownership before migration, recovery, or storage cleanup."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
from filelock import FileLock, Timeout
from sqlalchemy.engine import make_url


@contextmanager
def exclusive_workspace(settings):
    database = make_url(settings.database_url).database
    paths = [settings.uploads_dir.resolve() / ".owner.lock"]
    if database and database != ":memory:":
        paths.append(Path(database).resolve().with_name(Path(database).name + ".lock"))
    with ExitStack() as stack:
        try:
            for path in sorted(set(paths)):
                path.parent.mkdir(parents=True, exist_ok=True)
                stack.enter_context(FileLock(str(path), timeout=0))
        except Timeout as exc:
            raise RuntimeError("Database or upload storage is already owned by another application process") from exc
        yield
