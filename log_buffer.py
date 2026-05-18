import logging
from collections import deque

_buffer: deque[str] = deque(maxlen=500)


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _buffer.append(self.format(record))
        except Exception:
            pass


_handler = _BufferHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))


def install() -> None:
    logging.getLogger().addHandler(_handler)


def get_lines(n: int = 100) -> list[str]:
    lines = list(_buffer)
    return lines[-n:]
