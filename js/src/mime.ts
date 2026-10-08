// SPDX-License-Identifier: Apache-2.0
/**
 * The upload's MIME type by file extension: the same table as the Python SDK's
 * (statelock.client.transfer.MIME_TYPES; tests/test_client_transfer.py compares them),
 * so upload evidence does not depend on the SDK. Other extensions upload with no type.
 */

const MIME_TYPES: Record<string, string> = {
  ".7z": "application/x-7z-compressed",
  ".bmp": "image/bmp",
  ".csv": "text/csv",
  ".doc": "application/msword",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".eml": "message/rfc822",
  ".epub": "application/epub+zip",
  ".gif": "image/gif",
  ".gz": "application/gzip",
  ".heic": "image/heic",
  ".htm": "text/html",
  ".html": "text/html",
  ".ics": "text/calendar",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".js": "text/javascript",
  ".json": "application/json",
  ".md": "text/markdown",
  ".mp3": "audio/mpeg",
  ".mp4": "video/mp4",
  ".odp": "application/vnd.oasis.opendocument.presentation",
  ".ods": "application/vnd.oasis.opendocument.spreadsheet",
  ".odt": "application/vnd.oasis.opendocument.text",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".ppt": "application/vnd.ms-powerpoint",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".rtf": "application/rtf",
  ".svg": "image/svg+xml",
  ".tar": "application/x-tar",
  ".tex": "application/x-tex",
  ".tif": "image/tiff",
  ".tiff": "image/tiff",
  ".tsv": "text/tab-separated-values",
  ".txt": "text/plain",
  ".wav": "audio/wav",
  ".webp": "image/webp",
  ".xls": "application/vnd.ms-excel",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".xml": "application/xml",
  ".zip": "application/zip",
};

/** The MIME type for a file name, null for an extension not in the table (".pdf" alone has none, as in Python). */
export function mimeType(name: string): string | null {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? (MIME_TYPES[name.slice(dot).toLowerCase()] ?? null) : null;
}
