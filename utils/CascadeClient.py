# cascade_client.py
from __future__ import annotations
import requests
from urllib.parse import urlparse
import re
from typing import Tuple, Optional, Dict, Any, List

# ---------------- Your top-level sections that live under "ualberta" ----------------
folders: List[str] = [
    "about", "admissions-programs", "advancing-alberta", "artificial-intelligence",
    "camps", "campus-alberta", "campus-life", "congress-2021", "convocation",
    "current-students", "dean-test", "emergency", "events", "experiential-learning",
    "facilities", "faculties", "faculty-and-staff", "flight-ps752-memorial",
    "graduate-programs", "healthy-campuses", "impact", "incident-updates",
    "innovation-generator", "media-library", "news", "policies-procedures",
    "professional-development", "reporting", "search", "services", "truth-matters",
    "ukraine", "visitor-hub",
]

# ---------------- Base URL ----------------
def webcmsBaseUrl(testing: bool) -> str:
    return "https://dev.webcms.ualberta.ca" if testing else "https://webcms.ualberta.ca"

# ---------------- Name resolution ----------------
def stripHtmlSuffix(path: str) -> str:
    return re.sub(r"\.html?$", "", path)

def dropLangPrefix(path: str) -> str:
    # remove leading /en or /fr
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] in {"en", "fr"}:
        parts = parts[1:]
    return "/" + "/".join(parts)

def resolveSiteAndPage(liveUrl: str, foldersList: List[str] = folders) -> Tuple[str, str]:
    """
    Map a live https://www.ualberta.ca/* URL to (siteName, pagePath) for Cascade REST.
    Rule: if first segment (after /en or /fr) is in folders => site='ualberta'.
          Otherwise, first segment is the Cascade site name.
    Returns ('site', '/path/without/.html'; guarantees '/index' when needed).
    """
    parsedUrl = urlparse(liveUrl)
    if not parsedUrl.netloc.endswith("ualberta.ca"):
        raise ValueError(f"Not a ualberta.ca URL: {liveUrl}")

    path = stripHtmlSuffix(dropLangPrefix(parsedUrl.path))
    pathParts = [s for s in path.split("/") if s]
    if not pathParts:
        return "ualberta", "/index"

    firstSegment = pathParts[0]
    if firstSegment in foldersList:
        siteName = "ualberta"
        pagePath = "/" + "/".join(pathParts)
    else:
        siteName = firstSegment
        remainder = pathParts[1:] or ["index"]
        pagePath = "/" + "/".join(remainder)

    if pagePath.endswith("/"):
        pagePath += "index"
    return siteName, pagePath

# ---------------- URL builders ----------------
def buildReadUrl(liveUrl: str, testing: bool = True) -> str:
    base = webcmsBaseUrl(testing)
    siteName, pagePath = resolveSiteAndPage(liveUrl)
    return f"{base}/api/v1/read/page/{siteName}{pagePath}"

def buildEditUrl(testing: bool = True) -> str:
    base = webcmsBaseUrl(testing)
    return f"{base}/api/v1/edit"  # body must include {"asset": {...}}

def buildPublishUrl(liveUrl: str, testing: bool = True) -> str:
    base = webcmsBaseUrl(testing)
    siteName, pagePath = resolveSiteAndPage(liveUrl)
    return f"{base}/api/v1/publish/page/{siteName}{pagePath}"

# ---------------- High-level client ----------------
class CascadeClient:
    def __init__(self, apiKey: str, testing: bool = True):
        self.apiKey = apiKey
        self.testing = testing

    # ---- READ ----
    def read(self, liveUrl: str) -> requests.Response:
        url = buildReadUrl(liveUrl, self.testing)
        return requests.post(url, headers={"Authorization": f"Bearer {self.apiKey}"})

    # ---- EDIT ----
    def editAsset(self, asset: Dict[str, Any]) -> requests.Response:
        """
        Send {"asset": asset} to /edit.
        Typical flow: asset = self.read(...).json()["asset"]; mutate; editAsset(asset)
        """
        url = buildEditUrl(self.testing)
        return requests.post(url, headers={"Authorization": f"Bearer {self.apiKey}"},
                             json={"asset": asset})

    # ---- PUBLISH (separate function, as requested) ----
    def publish(self,
                liveUrl: str,
                destinations: Optional[List[str]] = None,
                unpublish: bool = False,
                checkPublishPermissions: Optional[bool] = None,
                publishSet: Optional[str] = None) -> requests.Response:
        """
        Queue a publish (or unpublish) for the resolved page.
        REST op: POST /api/v1/publish/{identifier}
        Body (optional): {"publishInformation": {...}}
        Notes:
          - 'success:true' in the response means 'queued', not 'finished'.
        """
        url = buildPublishUrl(liveUrl, self.testing)
        body: Dict[str, Any] = {}

        publishInfo: Dict[str, Any] = {}
        if destinations:
            publishInfo["destinations"] = destinations
        if publishSet:
            publishInfo["publishSet"] = publishSet
        if unpublish:
            publishInfo["unpublish"] = True
        if checkPublishPermissions is not None:
            publishInfo["checkPublishPermissions"] = checkPublishPermissions

        if publishInfo:
            body["publishInformation"] = publishInfo

        return requests.post(url, headers={"Authorization": f"Bearer {self.apiKey}"},
                             json=body if body else None)

    # ---- READ by site + path (for scripts that work with site/path directly) ----
    def readByPath(self, siteName: str, pagePath: str) -> requests.Response:
        """
        Read a page by Cascade site name and page path instead of a live URL.
        Useful when you already know the site/path and don't need URL resolution.
        e.g. readByPath("ualberta", "/about/index")
        """
        url = f"{webcmsBaseUrl(self.testing)}/api/v1/read/page/{siteName}{pagePath}"
        return requests.post(url, headers={"Authorization": f"Bearer {self.apiKey}"})

    def editPageByPath(self, siteName: str, pagePath: str, pageAsset: Dict[str, Any]) -> requests.Response:
        """
        Edit a page using the /edit/page/{site}{path} endpoint.

        This endpoint targets the page directly by path, which is more explicit
        than the generic /edit endpoint. Use this when you have the site name and
        path and want to update a specific page asset.

        Args:
            siteName:  Cascade site name (e.g. "arts")
            pagePath:  Page path (e.g. "/faculty-news/2015/august/my-page")
            pageAsset: The full page asset dict from a prior readByPath() call,
                       with your mutations applied. Should be page_json["asset"]["page"].

        Note: 'success: true' means the edit was accepted, not yet published.
        """
        url = f"{webcmsBaseUrl(self.testing)}/api/v1/edit/page/{siteName}{pagePath}"
        payload = {
            "asset": {
                "page":              pageAsset,
                "shouldBePublished": pageAsset.get("shouldBePublished", False),
                "shouldBeIndexed":   pageAsset.get("shouldBeIndexed", True),
            }
        }
        return requests.post(
            url,
            headers={"Authorization": f"Bearer {self.apiKey}", "Content-Type": "application/json"},
            json=payload,
        )


# ---------------- File asset operations ----------------
# Page-level operations are handled by the CascadeClient class above.
# These standalone functions cover file assets (images, PDFs, etc.)
# which the class doesn't currently support.

def cascadeReadFile(siteName: str, assetPath: str, apiKey: str, testing: bool = False) -> Dict[str, Any]:
    """
    Read a file asset (image, PDF, etc.) from Cascade by site name and path.
    Returns the parsed JSON response dict.
    Raises on non-200 or network errors.
    """
    url = f"{webcmsBaseUrl(testing)}/api/v1/read/file/{siteName}{assetPath}"
    res = requests.post(url, headers={"Authorization": f"Bearer {apiKey}"})
    res.raise_for_status()
    return res.json()


def cascadeWriteFile(siteName: str, assetPath: str, assetPayload: Dict[str, Any], apiKey: str, testing: bool = False) -> requests.Response:
    """
    Write (edit) a file asset back to Cascade.
    assetPayload should match the structure Cascade expects for a file edit.
    Raises on non-200 or network errors.
    """
    url = f"{webcmsBaseUrl(testing)}/api/v1/edit/file/{siteName}{assetPath}"
    res = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {apiKey}",
            "Content-Type": "application/json",
        },
        json={"asset": assetPayload},
    )
    res.raise_for_status()
    return res


# ---------------- Byte conversion helpers ----------------
# Cascade stores image data as Java-signed integers (-128 to 127).
# These two functions convert to/from standard Python bytes.

def decodeCascadeFileBytes(signed: List[int]) -> bytes:
    """Convert Java-signed integers from Cascade into proper unsigned bytes."""
    return bytes((b + 256) % 256 for b in signed)


def encodeCascadeFileBytes(imgBytes: bytes) -> List[int]:
    """Convert unsigned bytes back into Java-signed integers for a Cascade write payload."""
    return [(b if b < 128 else b - 256) for b in imgBytes]


def cascadeReadFileBytes(siteName: str, assetPath: str, apiKey: str, testing: bool = False) -> bytes:
    """
    Read a Cascade file asset and return just the image bytes.
    Handles the Java-signed integer conversion automatically.
    Raises RuntimeError if the asset contains no data.
    """
    data = cascadeReadFile(siteName, assetPath, apiKey, testing=testing)
    file_asset = data["asset"]["file"]
    signed = file_asset.get("data") or file_asset.get("fileBytes") or []
    if not signed:
        raise RuntimeError(f"No file bytes returned for {assetPath}")
    return decodeCascadeFileBytes(signed)
