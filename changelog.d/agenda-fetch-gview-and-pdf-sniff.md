Agenda text fetching now unwraps Granicus `AgendaViewer.php` redirects to the Google Docs viewer
(fetching the same-origin document it wraps) and recognises a PDF by its `%PDF-` signature when the
content type and URL do not say so, so those agendas are no longer rejected as
placeholder-or-low-quality-html.
