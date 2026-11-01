# Download integrity

MApp 0.1.2 requests video ranges of at most 4 MiB with
`X-MCore-Integrity: sha256`. MCore waits for the corresponding torrent pieces,
buffers the range, and returns its SHA-256 in `X-MCore-SHA256` alongside the
standard 206, Content-Range and Content-Length headers. Authentication and
TLS remain required. An unsupported algorithm, missing range or oversized
range returns 400; unavailable data returns 503 before a successful response.

The client validates the range, length and hash before writing, then flushes
the file before advancing its saved download offset. It retries failed
integrity checks up to three attempts and pauses on persistent failure.
Existing clients can continue requesting ordinary streaming ranges.

This verifies newly transferred bytes, not the media container's validity
or previously saved data. Tests cover real torrent range digests, malformed
requests, unavailable pieces, and Android rejection/retry behavior.
