"""Map exact source code points to Qt's normalized UTF-16 document offsets."""

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit


def qt_text_and_positions(source):
    # QTextDocument normalizes CRLF/CR to one paragraph separator. Build the
    # boundary map ourselves rather than searching for a quote in Qt's text.
    pieces, positions, units, index = [], [0], 0, 0
    while index < len(source):
        char = source[index]
        if char == "\r" and source[index:index + 2] == "\r\n":
            pieces.append("\n")
            units += 1
            positions.extend((units, units))
            index += 2
            continue
        normalized = "\n" if char in ("\r", "\u2028", "\u2029") else char
        pieces.append(normalized)
        units += len(normalized.encode("utf-16-le")) // 2
        positions.append(units)
        index += 1
    return "".join(pieces), tuple(positions)


def set_evidence(editor, source, spans):
    """Validate ALL spans before showing text; mismatch never falls back to find()."""
    editor.clear()
    editor.setExtraSelections([])
    for span in spans:
        if not (0 <= span.start < span.end <= len(source)) or source[span.start:span.end] != span.quote:
            raise ValueError("Invalid evidence offsets")
    normalized, positions = qt_text_and_positions(source)
    editor.setPlainText(normalized)
    if editor.toPlainText() != normalized:
        editor.clear()
        raise ValueError("Unsupported Qt text normalization")
    selections = []
    for span in spans:
        selection = QTextEdit.ExtraSelection()
        selection.cursor = QTextCursor(editor.document())
        selection.cursor.setPosition(positions[span.start])
        selection.cursor.setPosition(positions[span.end], QTextCursor.MoveMode.KeepAnchor)
        selection.format = QTextCharFormat()
        selection.format.setBackground(QColor("#ffe499"))
        selection.format.setForeground(QColor("#16202a"))
        selections.append(selection)
    editor.setExtraSelections(selections)
