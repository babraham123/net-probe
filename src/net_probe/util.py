# Logging and atomic file writes.


def log(message):
  print(message, flush=True)


def write(path, text):
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(".tmp")
  tmp.write_text(text)
  tmp.chmod(0o644)
  tmp.replace(path)
