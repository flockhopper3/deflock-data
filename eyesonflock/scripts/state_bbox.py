"""Approximate US-state bounding boxes for geocode plausibility checks.

Shared by 02a_google_geocode.py (reject Google results that land outside the
declared state's bbox) and 05_audit_geocoding.py (report outliers in the
post-pipeline audit). Margins are generous (~1 degree past the true extents)
to avoid flagging legitimate near-border matches.
"""

# state USPS code -> (min_lat, max_lat, min_lng, max_lng)
STATE_BBOX: dict[str, tuple[float, float, float, float]] = {
    "AL": (30.0, 35.5, -89.0, -84.5),
    "AK": (51.0, 72.0, -180.0, -129.0),
    "AZ": (31.0, 37.5, -115.0, -108.5),
    "AR": (33.0, 36.6, -94.7, -89.5),
    "CA": (32.0, 42.5, -125.0, -114.0),
    "CO": (36.5, 41.5, -109.5, -101.5),
    "CT": (40.5, 42.5, -73.8, -71.5),
    "DE": (38.3, 40.0, -76.0, -74.8),
    "DC": (38.7, 39.1, -77.2, -76.8),
    "FL": (24.0, 31.5, -88.0, -79.5),
    "GA": (30.3, 35.3, -86.0, -80.5),
    "HI": (18.5, 22.5, -160.5, -154.5),
    "ID": (41.5, 49.5, -117.5, -111.0),
    "IL": (36.5, 42.7, -91.7, -87.0),
    "IN": (37.5, 42.0, -88.2, -84.5),
    "IA": (40.3, 43.7, -96.7, -90.0),
    "KS": (36.8, 40.3, -102.3, -94.5),
    "KY": (36.3, 39.3, -89.7, -81.7),
    "LA": (28.5, 33.3, -94.3, -88.5),
    "ME": (43.0, 47.7, -71.2, -66.8),
    "MD": (37.8, 39.8, -79.7, -74.8),
    "MA": (41.0, 43.0, -73.7, -69.8),
    "MI": (41.5, 48.5, -90.5, -82.0),
    "MN": (43.3, 49.5, -97.5, -89.3),
    "MS": (30.0, 35.3, -91.8, -88.0),
    "MO": (35.8, 40.8, -95.8, -89.0),
    "MT": (44.3, 49.3, -116.3, -104.0),
    "NE": (39.8, 43.3, -104.3, -95.3),
    "NV": (34.8, 42.3, -120.3, -114.0),
    "NH": (42.5, 45.5, -72.7, -70.5),
    "NJ": (38.8, 41.5, -75.7, -73.8),
    "NM": (31.2, 37.3, -109.3, -103.0),
    "NY": (40.3, 45.3, -80.0, -71.5),
    "NC": (33.5, 36.8, -84.5, -75.3),
    "ND": (45.8, 49.3, -104.3, -96.5),
    "OH": (38.3, 42.0, -85.0, -80.3),
    "OK": (33.5, 37.3, -103.3, -94.3),
    "OR": (41.8, 46.5, -124.8, -116.3),
    "PA": (39.5, 42.5, -80.7, -74.5),
    "RI": (41.0, 42.3, -72.0, -71.0),
    "SC": (32.0, 35.5, -83.5, -78.3),
    "SD": (42.3, 46.0, -104.3, -96.3),
    "TN": (34.8, 36.8, -90.5, -81.5),
    "TX": (25.5, 36.7, -106.8, -93.3),
    "UT": (36.8, 42.3, -114.3, -108.8),
    "VT": (42.5, 45.3, -73.7, -71.3),
    "VA": (36.3, 39.7, -83.8, -75.3),
    "WA": (45.5, 49.3, -125.0, -116.8),
    "WV": (37.0, 40.8, -82.8, -77.5),
    "WI": (42.3, 47.3, -93.0, -86.5),
    "WY": (40.8, 45.3, -111.3, -104.0),
    "PR": (17.5, 18.7, -67.5, -65.0),
}


def is_in_state_bbox(lat: float, lng: float, state: str | None) -> bool:
    """Return True if (lat, lng) is inside the state's bounding box.

    Unknown or missing state returns True — we don't have information to
    reject, so we let the coords through. Callers that want to be strict
    should check `state in STATE_BBOX` themselves.
    """
    if not state or state not in STATE_BBOX:
        return True
    min_lat, max_lat, min_lng, max_lng = STATE_BBOX[state]
    return min_lat <= lat <= max_lat and min_lng <= lng <= max_lng
