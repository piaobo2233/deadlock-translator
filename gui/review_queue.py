"""Compatibility review entry: always use the main dictionary workspace."""
def open_review(app):
    app.open_page('dictionary')
    app.dictionary.pick('review')
    return app.dictionary.editor
