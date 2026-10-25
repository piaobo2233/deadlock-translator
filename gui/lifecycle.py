"""Page-owned callbacks: navigation and window destruction cancel pending work."""


class PageCallbacks:
    def __init__(self, root):
        self.root = root
        self._pending = set()
        self._generation = 0
        self._owners = {}

    def schedule(self, delay, callback, *, owner=None):
        generation = self._generation
        token = None

        def run():
            active = token in self._pending
            self._pending.discard(token)
            self._owners.pop(token, None)
            if active and generation == self._generation:
                callback()

        token = self.root.after(delay, run)
        self._pending.add(token)
        self._owners[token] = owner
        return token

    def cancel_owner(self, owner):
        for token in list(self._pending):
            if self._owners.get(token) == owner:
                self.cancel(token)

    def cancel(self, token):
        self._pending.discard(token)
        self._owners.pop(token, None)
        try:
            self.root.after_cancel(token)
        except Exception:
            pass

    def cancel_all(self):
        self._generation += 1
        pending, self._pending = self._pending, set()
        self._owners.clear()
        for token in pending:
            try:
                self.root.after_cancel(token)
            except Exception:
                # Tk may already be shutting down; generation also invalidates callbacks.
                pass
