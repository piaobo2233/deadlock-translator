# Japanese quality fallback

`jpn_quality.traineddata` is derived from the official `tessdata_best` Japanese model at commit `e12c65a915945e4c28e237a9b52bc4a8f39a0cec`.

Source: https://github.com/tesseract-ocr/tessdata_best/tree/e12c65a915945e4c28e237a9b52bc4a8f39a0cec

Modification: the upstream `combine_tessdata -c jpn.traineddata` tool compacted the LSTM weights to integers. The resulting file was renamed, preserving the existing fast model. Input/output hashes and the converter hash are recorded in `quality-provenance.json`; the original upstream license is preserved verbatim in `QUALITY-LICENSE` (Apache-2.0).

The fast combined recognizer establishes an initial script hint. Script-specific recognition is prioritized when the script is known; an independently verified language hint can skip the combined pass only while complete glyph pixels still match. Japanese body reading and independent source verification use the quality model with 4x enlargement when present. Script reads prefer nearest-neighbor enlargement, with one bounded cubic alternative for video-compression artifacts; Korean uses 4x and Russian 3x enlargement. Confidence, whole-body agreement, source ownership and temporal confirmation requirements remain in force. Script contexts do not apply the English user-word list. Missing this optional quality model falls back to the existing Japanese model.
