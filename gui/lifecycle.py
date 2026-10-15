"""Page-owned callbacks: navigation and window destruction cancel pending work."""


class PageCallbacks:
    def __init__(self, root):
        self.root = root
        self._pending = set()
        self._generation = 0

    def schedule(self, delay, callback):
        generation = self._generation
        token = None

        def run():
            self._pending.discard(token)
            if generation == self._generation:
                callback()

        token = self.root.after(delay, run)
        self._pending.add(token)
        return token

    def cancel_all(self):
        self._generation += 1
        pending, self._pending = self._pending, set()
        for token in pending:
            try:
                self.root.after_cancel(token)
            except Exception:
                # Tk may already be shutting down; generation also invalidates callbacks.
                pass
