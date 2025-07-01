#!/usr/bin/env python3
"""
RealWorld API Implementation - Single File Demo Server

⚠️  THIS IS PROBABLY NOT THE PROJECT YOU ARE LOOKING FOR  ⚠️

This is the opposite of what you'd expect in a real-world project: a single-file,
in-memory, framework-free implementation of the RealWorld API specification.

## Purpose
Demo backend for testing/development that manages all data in memory.
For a regular Python implementation that follows commonly accepted best practices, you can check:
https://github.com/c4ffein/realworld-django-ninja/

## Key Design Decisions
- **In-memory storage**: Data persists only during server runtime
  - TODO Save temporary data during graceful shutdown
- **Per-browser isolation**: Uses an additional undocumented cookie to separate data between different browsers
  - As the Origin header is included for POST requests regardless of origin, use it against CSRF for the
    - regitration: POST on /users
    - login: POST on /users/login
  - Any other route is safe against CSRF as we are using Token in headers and not a cookie
- **Zero dependencies**: Python standard library only
- **Single file**: Entire server implementation in one module
- **Simple logging**: Of most operations  TODO

## Rate Limiting
- Applied per browser session (not per RealWorld user account) via the UNDOCUMENTED_DEMO_SESSION cookie
  - Prevents the same IPv4 or IPv6 range to use too many different UNDOCUMENTED_DEMO_SESSION
    - That way it doesn't overflow the pool of the currently saved sessions
- There are limits on the objects that will be saved in memory

## Deploy
- You should also rate limit per IPv4 address and IPv6 range through a reverse proxy
- You should still limit the max body size per request through a reverse proxy

## Development Notes
- Vibe coded with Claude Code
- Tested against the regular test suites
- Usable as a demo backend, if risking to lose data is an acceptable tradeoff
- Working on this project was refreshing because the implementation approach differed a lot from typical web dev:
  It allowed for design decisions based on different constraints than these of more standard web projects

⚠️  DO NOT BASE NON-DEMO PROJECTS ON THIS SPECIFIC IMPLEMENTATION  ⚠️
"""

import hashlib
import json
import re
import time
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from os import getenv
from time import time_ns
from typing import Dict, List, Optional, Tuple
from unittest import TestCase
from urllib.parse import parse_qs, urlparse


# security
DISABLE_ISOLATION_MODE = getenv("DISABLE_ISOLATION_MODE", "FALSE").lower() == "true"
MAX_SESSIONS = int(getenv("MAX_SESSIONS") or 300)
BYPASS_ORIGIN_CHECK = getenv("BYPASS_ORIGIN_CHECK", "FALSE").lower() == "true"
ALLOWED_ORIGINS = getenv("ALLOWED_ORIGINS", "").split(";")
if ALLOWED_ORIGINS == [""] and not BYPASS_ORIGIN_CHECK:
    raise ValueError("ALLOWED_ORIGINS varenv should be set if BYPASS_ORIGIN_CHECK isn't")
# max lengths for keys and objects per session
MAX_ID_LEN = int(getenv("MAX_ID_LEN", 64))
MAX_USERS_PER_SESSION = int(getenv("MAX_USERS_PER_SESSION", 60))
MAX_ARTICLES_PER_SESSION = int(getenv("MAX_ARTICLES_PER_SESSION", 10))
MAX_COMMENTS_PER_SESSION = int(getenv("MAX_COMMENTS_PER_SESSION", 20))
MAX_FOLLOWS_PER_SESSION = int(getenv("MAX_FOLLOWS_PER_SESSION", 100))
MAX_FAVORITES_PER_SESSION = int(getenv("MAX_FAVORITES_PER_SESSION", 100))
# max lengths for fields in models
MAX_LEN_USER_EMAIL = int(getenv("MAX_LEN_USER_EMAIL", 100))
MAX_LEN_USER_USERNAME = int(getenv("MAX_LEN_USER_USERNAME", 60))
MAX_LEN_USER_PASSWORD = int(getenv("MAX_LEN_USER_PASSWORD", 60))
MAX_LEN_USER_BIO = int(getenv("MAX_LEN_USER_BIO", 400))
MAX_LEN_USER_IMAGE = int(getenv("MAX_LEN_USER_IMAGE", 200))
MAX_LEN_ARTICLE_TITLE = int(getenv("MAX_LEN_ARTICLE_TITLE", 100))
MAX_LEN_ARTICLE_DESCRIPTION = int(getenv("MAX_LEN_ARTICLE_DESCRIPTION", 300))
MAX_LEN_ARTICLE_BODY = int(getenv("MAX_LEN_ARTICLE_BODY", 3000))
MAX_LEN_ARTICLE_TAG_LIST = int(getenv("MAX_LEN_ARTICLE_TAG_LIST", 10))
MAX_LEN_ARTICLE_TAG_LEN = int(getenv("MAX_LEN_ARTICLE_TAG_LEN", 20))
MAX_LEN_COMMENT_BODY = int(getenv("MAX_LEN_COMMENT_BODY", 300))
# computed naive total size - not counting the links like favorites and followed
NAIVE_SIZE_USER = MAX_LEN_USER_EMAIL + MAX_LEN_USER_USERNAME + MAX_LEN_USER_PASSWORD + MAX_LEN_USER_BIO
NAIVE_SIZE_USER += MAX_LEN_USER_IMAGE
NAIVE_SIZE_TAG = MAX_LEN_ARTICLE_TAG_LIST + MAX_LEN_ARTICLE_TAG_LEN
NAIVE_SIZE_ARTICLE = MAX_LEN_ARTICLE_TITLE + MAX_LEN_ARTICLE_DESCRIPTION + MAX_LEN_ARTICLE_BODY + NAIVE_SIZE_TAG
NAIVE_SIZE_COMMENT = MAX_LEN_COMMENT_BODY
NAIVE_SIZE_SESSION_USER = NAIVE_SIZE_USER * MAX_USERS_PER_SESSION
NAIVE_SIZE_SESSION_ARTICLE = NAIVE_SIZE_ARTICLE * MAX_ARTICLES_PER_SESSION
NAIVE_SIZE_SESSION_COMMENT = NAIVE_SIZE_COMMENT * MAX_COMMENTS_PER_SESSION
NAIVE_SIZE_SESSION = NAIVE_SIZE_SESSION_USER + NAIVE_SIZE_SESSION_ARTICLE + NAIVE_SIZE_SESSION_COMMENT
NAIVE_SIZE_TOTAL = NAIVE_SIZE_SESSION * MAX_ARTICLES_PER_SESSION  # TODO log total result on start


def normalize_id(value):
    if type(value) == int:
        value = str(value)
    if type(value) == str:
        if len(value) > MAX_ID_LEN:
            raise ValueError("id is too long")
        return value
    raise ValueError("id must be an int or an str")


class InMemoryModel:
    """
    when rolling with new ids, we may safe-delete so we don't break any link in the storage (maybe through a callback)
    won't implement for now as the ROI isn't really there
    """
    def __init__(self, max_count):
        self.max_count: int = max_count
        self.objects: Dict[str, object] = {}
        self.last_accessed_ids: List[str] = []  # perf ok because of the objects per model per session limit
        self.current_id_counter = 1
        if self.max_count <= 0:
            raise ValueError("invalid value for max_count")

    def add(self, obj):
        if len(str(self.current_id_counter)) > MAX_ID_LEN:
            raise ValueError("cannot allocate id: we reached MAX_ID_LEN limit")
        self.objects[str(self.current_id_counter)] = obj
        obj["id"] = str(self.current_id_counter)
        self.current_id_counter += 1
        if len(self.objects) > self.max_count:
            del self.objects[self.last_accessed_ids[0]]
            self.last_accessed_ids = self.last_accessed_ids[1:] + [obj["id"]]
        else:
            self.last_accessed_ids.append(obj["id"])
        return obj

    def get(self, _id):
        _id = normalize_id(_id)
        if _id not in self.objects:
            return None
        self.last_accessed_ids = [*(e for e in self.last_accessed_ids if e != _id), _id]
        return self.objects[_id]

    def keys(self):
        return self.objects.keys()

    def values(self):
        return self.objects.values()

    def items(self):
        return self.objects.items()

    def delete(self, _id):
        _id = normalize_id(_id)
        if _id in self.objects:
            del self.objects[_id]
            self.last_accessed_ids = [cid for cid in self.last_accessed_ids if cid != _id]
            return True
        return False


class InMemoryLinks:
    def __init__(self, max_count):
        self.max_count: int = max_count
        self.links: List[Tuple[int, int]] = []  # cheaper implem to limit global number of links and wipe oldest

    def add(self, source, target):
        source, target = normalize_id(source), normalize_id(target)
        if self.max_count == 0:
            return
        _index = self.links.index((source, target)) if (source, target) in self.links else None
        if _index is not None:
            self.links = [*self.links[:_index], *self.links[_index+1:], (source, target)]
        elif len(self.links) >= self.max_count:
            self.links = [*self.links[1:], (source, target)]
        else:
            self.links = [*self.links, (source, target)]

    def remove(self, source, target):
        source, target = normalize_id(source), normalize_id(target)
        _index = self.links.index((source, target)) if (source, target) in self.links else None
        if _index is not None:
            self.links = [*self.links[:_index], *self.links[_index+1:]]

    def is_linked(self, source, target):
        source, target = normalize_id(source), normalize_id(target)
        return (source, target) in self.links

    def targets_for_source(self, wanted_source):
        return [target for source, target in self.links if source == normalize_id(wanted_source)]

    def sources_for_target(self, wanted_target):
        return [source for source, target in self.links if target == normalize_id(wanted_target)]

    def delete_source(self, source_to_delete):
        self.links = [(source, target) for source, target in self.links if source != normalize_id(source_to_delete)]

    def delete_target(self, target_to_delete):
        self.links = [(source, target) for source, target in self.links if target != normalize_id(target_to_delete)]


class InMemoryStorage:
    """In-memory storage for all data"""

    def __init__(self):
        self.users = InMemoryModel(max_count=MAX_USERS_PER_SESSION)
        self.articles = InMemoryModel(max_count=MAX_ARTICLES_PER_SESSION)
        self.comments = InMemoryModel(max_count=MAX_COMMENTS_PER_SESSION)
        self.follows = InMemoryLinks(max_count=MAX_FOLLOWS_PER_SESSION)  # user_id -> followed user_ids
        self.favorites = InMemoryLinks(max_count=MAX_FAVORITES_PER_SESSION)  # user_id -> favorited article_ids


class _StorageContainer:
    """Remove storage for the least used session once max_sessions is reached"""

    def __init__(self, disable_isolation_mode=DISABLE_ISOLATION_MODE, max_sessions=MAX_SESSIONS):
        self.DISABLE_ISOLATION_MODE = disable_isolation_mode
        self.MAX_SESSIONS = max_sessions
        self.heap = []
        self.index_map = {}

    def _push(self, priority, obj_id, data=None):
        """Push an item onto the heap"""
        index = len(self.heap)
        item = [priority, obj_id, data, index]  # Include index in item
        self.heap.append(item)
        self.index_map[obj_id] = index
        self._sift_up(index)

    def _pop(self):
        """Pop the smallest item from the heap"""
        if not self.heap:
            return None
        # Remove from index map
        root_item = self.heap[0]
        del self.index_map[root_item[1]]
        if len(self.heap) == 1:
            return self.heap.pop()
        # Move last item to root
        last_item = self.heap.pop()
        self.heap[0] = last_item
        self.heap[0][3] = 0  # Update index
        self.index_map[last_item[1]] = 0
        self._sift_down(0)
        return root_item

    def _update_priority(self, obj_id, new_priority):
        """Update the priority of an existing item"""
        if obj_id not in self.index_map:
            raise ValueError(f"Object {obj_id} not found in heap")
        index = self.index_map[obj_id]
        old_priority = self.heap[index][0]
        self.heap[index][0] = new_priority
        if new_priority < old_priority:
            self._sift_up(index)
        elif new_priority > old_priority:
            self._sift_down(index)

    def _sift_up(self, index):
        """Restore heap property upward"""
        while index > 0:
            parent_index = (index - 1) // 2
            if self.heap[index][0] >= self.heap[parent_index][0]:
                break
            # Swap items
            self._swap(index, parent_index)
            index = parent_index

    def _sift_down(self, index):
        """Restore heap property downward"""
        while True:
            smallest = index
            left_child = 2 * index + 1
            right_child = 2 * index + 2
            if (left_child < len(self.heap) and self.heap[left_child][0] < self.heap[smallest][0]):
                smallest = left_child
            if (right_child < len(self.heap) and self.heap[right_child][0] < self.heap[smallest][0]):
                smallest = right_child
            if smallest == index:
                break
            self._swap(index, smallest)
            index = smallest

    def _swap(self, i, j):
        """Swap two items and update their indices"""
        self.index_map[self.heap[i][1]], self.index_map[self.heap[j][1]] = j, i  # Update index map
        self.heap[i][3], self.heap[j][3] = j, i  # Update indices in items
        self.heap[i], self.heap[j] = self.heap[j], self.heap[i]  # Swap items

    def get_storage(self, identifier):
        if self.DISABLE_ISOLATION_MODE:
            if not self.heap:
                self.heap.append(InMemoryStorage())  # Not using expected implem
            return self.heap[0]  # Heap is not filled with the expected lists
        if not identifier:  # UNDOCUMENTED_DEMO_SESSION is not defined, but what if the logged-in user deleted it?
            return InMemoryStorage()  # quick and dirty solution to prevent overwriting
        storage_container_index = self.index_map.get(identifier)
        if storage_container_index is None:
            if len(self.index_map) >= self.MAX_SESSIONS:
                self._pop()
            self._push(time_ns(), identifier, data=InMemoryStorage())
            return self.heap[self.index_map.get(identifier)][2]
        r = self.heap[storage_container_index][2]
        self._update_priority(identifier, time_ns())
        return r


storage_container = _StorageContainer()


def generate_slug(title: str) -> str:
    """Generate URL-friendly slug from title"""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-")


def hash_password(password: str) -> str:
    """Simple password hashing"""
    return hashlib.sha256(password.encode()).hexdigest()


def generate_token(user_id: str) -> str:
    """Generate JWT-like token (simplified)"""
    payload = f"{user_id}:{int(time.time())}"
    return f"token_{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def verify_token(token: str, storage: InMemoryStorage) -> Optional[int]:
    """Verify token and return user_id if valid"""
    if not token or not token.startswith("token_"):
        return None
    # in a real implementation, you'd decode the JWT => for simplicity, we'll store token->user_id mapping
    return next((user_id for user_id, user in storage.users.items() if user.get("token") == token), None)


def get_user_by_email(email: str, storage: InMemoryStorage) -> Optional[Dict]:
    """Find user by email"""
    return next((user for user in storage.users.values() if user["email"] == email), None)


def get_user_by_username(username: str, storage: InMemoryStorage) -> Optional[Dict]:
    """Find user by username"""
    return next((user for user in storage.users.values() if user["username"] == username), None)


def get_article_by_slug(slug: str, storage: InMemoryStorage) -> Optional[Dict]:
    """Find article by slug"""
    return next((article for article in storage.articles.values() if article["slug"] == slug), None)


def format_datetime(dt: datetime) -> str:
    """Format datetime to ISO 8601"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def get_current_time() -> str:
    """Get current time in ISO format"""
    return format_datetime(datetime.now(timezone.utc))


def create_user_response(user: Dict, include_token: bool = True) -> Dict:
    """Create user response format"""
    response = {
        "username": user["username"],
        "email": user["email"],
        "bio": user.get("bio", ""),
        "image": user.get("image", "https://api.realworld.io/images/smiley-cyrus.jpeg"),
    }
    if include_token:
        response["token"] = user.get("token", "")
    return response


def create_profile_response(user: Dict, storage: InMemoryStorage, current_user_id: Optional[str] = None) -> Dict:
    """Create profile response format"""
    following = False
    if current_user_id:
        following = storage.follows.is_linked(current_user_id, user["id"])

    return {
        "username": user["username"],
        "bio": user.get("bio", ""),
        "image": user.get("image", "https://api.realworld.io/images/smiley-cyrus.jpeg"),
        "following": following,
    }


def create_article_response(article: Dict, storage: InMemoryStorage, current_user_id: Optional[str] = None) -> Dict:
    """Create article response format"""
    author = storage.users.get(article["author_id"])
    favorited = False
    if current_user_id:
        favorited = storage.favorites.is_linked(current_user_id, article["id"])

    favorites_count = len(storage.favorites.sources_for_target(article["id"]))

    return {
        "slug": article["slug"],
        "title": article["title"],
        "description": article["description"],
        "body": article["body"],
        "tagList": sorted(article["tagList"]),
        "createdAt": article["createdAt"],
        "updatedAt": article["updatedAt"],
        "favorited": favorited,
        "favoritesCount": favorites_count,
        "author": create_profile_response(author, storage, current_user_id),
    }


def create_comment_response(comment: Dict, storage: InMemoryStorage, current_user_id: Optional[str] = None) -> Dict:
    """Create comment response format"""
    author = storage.users.get(comment["author_id"])

    return {
        "id": comment["id"],
        "createdAt": comment["createdAt"],
        "updatedAt": comment["updatedAt"],
        "body": comment["body"],
        "author": create_profile_response(author, storage, current_user_id),
    }


class RealWorldHandler(BaseHTTPRequestHandler):
    """HTTP request handler for RealWorld API"""

    def do_GET(self):
        """Handle GET requests"""
        try:
            self._handle_request("GET")
        except Exception as e:
            self._send_error(500, {"errors": {"body": [str(e)]}})

    def do_POST(self):
        """Handle POST requests"""
        try:
            self._handle_request("POST")
        except Exception as e:
            self._send_error(500, {"errors": {"body": [str(e)]}})

    def do_PUT(self):
        """Handle PUT requests"""
        try:
            self._handle_request("PUT")
        except Exception as e:
            self._send_error(500, {"errors": {"body": [str(e)]}})

    def do_DELETE(self):
        """Handle DELETE requests"""
        try:
            self._handle_request("DELETE")
        except Exception as e:
            self._send_error(500, {"errors": {"body": [str(e)]}})

    def _handle_request(self, method: str):
        """Route request to appropriate handler"""
        ip_address = self.request.getpeername()[0]  # TODO Use this or X-Forwarded-For / X-Real-IP depending on setup + document
        storage = storage_container.get_storage(self._get_demo_session_cookie())
        parsed = urlparse(self.path)
        path = parsed.path
        query_params = parse_qs(parsed.query)
        # Remove leading slash and split path
        path_parts = path.strip("/").split("/")
        # Get authorization header
        auth_header = self.headers.get("Authorization", "")
        token = auth_header.replace("Token ", "") if auth_header.startswith("Token ") else None
        current_user_id = verify_token(token, storage) if token else None
        # Route to handlers
        if method == "POST" and path == "/users":
            if not self._check_csrf_protection():
                return self._send_error(403, {"errors": {"body": ["Origin header required for CSRF protection"]}})
            self._handle_register(storage)
        elif method == "POST" and path == "/users/login":
            if not self._check_csrf_protection():
                return self._send_error(403, {"errors": {"body": ["Origin header required for CSRF protection"]}})
            self._handle_login(storage)
        elif method == "GET" and path == "/user":
            self._handle_get_current_user(storage, current_user_id)
        elif method == "PUT" and path == "/user":
            self._handle_update_user(storage, current_user_id)
        elif method == "GET" and path_parts[0] == "profiles" and len(path_parts) == 2:
            self._handle_get_profile(storage, path_parts[1], current_user_id)
        elif method == "POST" and len(path_parts) == 3 and path_parts[0] == "profiles" and path_parts[2] == "follow":
            self._handle_follow_user(storage, path_parts[1], current_user_id)
        elif method == "DELETE" and len(path_parts) == 3 and path_parts[0] == "profiles" and path_parts[2] == "follow":
            self._handle_unfollow_user(storage, path_parts[1], current_user_id)
        elif method == "GET" and path == "/articles":
            self._handle_list_articles(storage, query_params, current_user_id)
        elif method == "GET" and path == "/articles/feed":
            self._handle_articles_feed(storage, query_params, current_user_id)
        elif method == "POST" and path == "/articles":
            self._handle_create_article(storage, current_user_id)
        elif method == "GET" and len(path_parts) == 2 and path_parts[0] == "articles":
            self._handle_get_article(storage, path_parts[1], current_user_id)
        elif method == "PUT" and len(path_parts) == 2 and path_parts[0] == "articles":
            self._handle_update_article(storage, path_parts[1], current_user_id)
        elif method == "DELETE" and len(path_parts) == 2 and path_parts[0] == "articles":
            self._handle_delete_article(storage, path_parts[1], current_user_id)
        elif method == "POST" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "favorite":
            self._handle_favorite_article(storage, path_parts[1], current_user_id)
        elif (
            method == "DELETE" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "favorite"
        ):
            self._handle_unfavorite_article(storage, path_parts[1], current_user_id)
        elif method == "GET" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "comments":
            self._handle_get_comments(storage, path_parts[1], current_user_id)
        elif method == "POST" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "comments":
            self._handle_create_comment(storage, path_parts[1], current_user_id)
        elif (
            method == "DELETE" and len(path_parts) == 4 and path_parts[0] == "articles" and path_parts[2] == "comments"
        ):
            self._handle_delete_comment(storage, path_parts[1], int(path_parts[3]), current_user_id)
        elif method == "GET" and path == "/tags":
            self._handle_get_tags(storage)
        else:
            self._send_error(404, {"errors": {"body": ["Not found"]}})

    def _get_request_body(self) -> Dict:
        """Parse JSON request body"""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length).decode("utf-8")
        return json.loads(body) if body else {}

    def _get_demo_session_cookie(self) -> Optional[str]:
        """Get UNDOCUMENTED_DEMO_SESSION cookie value"""
        cookie_header = self.headers.get("Cookie", "")
        if "UNDOCUMENTED_DEMO_SESSION=" not in cookie_header:
            return None
        # Extract cookie value (simple parsing)
        for cookie in cookie_header.split(";"):
            cookie = cookie.strip()
            if cookie.startswith("UNDOCUMENTED_DEMO_SESSION="):
                return cookie.split("=", 1)[1]
        return None

    def _send_response(self, status_code: int, data: Dict, demo_session_id: Optional[uuid.UUID] = None):
        """Send JSON response"""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        if demo_session_id:
            self.send_header("Set-Cookie", f"UNDOCUMENTED_DEMO_SESSION={demo_session_id}; Path=/")
        self.end_headers()
        response_body = json.dumps(data, indent=2)
        self.wfile.write(response_body.encode("utf-8"))

    def _send_error(self, status_code: int, error_data: Dict):
        """Send error response"""
        self._send_response(status_code, error_data)

    def _check_csrf_protection(self) -> bool:
        """Check CSRF protection using Origin header for POST requests"""
        return BYPASS_ORIGIN_CHECK or (self.headers.get("Origin") in ALLOWED_ORIGINS)

    def _require_auth(self, current_user_id: Optional[int]) -> int:
        """Require authentication, return user_id or raise error"""
        if current_user_id is None:
            self._send_error(401, {"errors": {"body": ["Unauthorized"]}})
            raise Exception("Unauthorized")
        return current_user_id

    # Auth endpoints

    def _handle_register(self, storage: InMemoryStorage):
        """POST /users - Register new user"""
        data = self._get_request_body()
        user_data = data.get("user", {})
        email = user_data.get("email")
        username = user_data.get("username")
        password = user_data.get("password")
        if not all([email, username, password]):
            self._send_error(422, {"errors": {"body": ["Email, username and password are required"]}})
            return
        max_lens = ((email, MAX_LEN_USER_EMAIL), (username, MAX_LEN_USER_USERNAME), (password, MAX_LEN_USER_PASSWORD))
        if not all(type[d] == str for d in (email, username, password)) and not all(len(d) <= l for d, l in max_lens):
            err_str = "Email, username and password are expected as strings of length less than "
            err_str += f"{MAX_LEN_USER_EMAIL}, {MAX_LEN_USER_USERNAME}, and {MAX_LEN_USER_PASSWORD}, respectively"
            self._send_error(422, {"errors": {"body": [err_str]}})
            return
        # Check if user already exists
        if get_user_by_email(email, storage) or get_user_by_username(username, storage):
            self._send_error(409, {"errors": {"body": ["User already exists"]}})
            return
        # Create new user
        user = {
            "email": email,
            "username": username,
            "password": hash_password(password),
            "bio": "",
            "image": "https://api.realworld.io/images/smiley-cyrus.jpeg",
            "createdAt": get_current_time(),
        }
        # Add user and get the auto-assigned ID
        user = storage.users.add(user)
        user_id = user["id"]
        # Generate token after we have the user_id
        token = generate_token(user_id)
        user["token"] = token
        demo_session_id = None if self._get_demo_session_cookie() else uuid.uuid4()
        self._send_response(201, {"user": create_user_response(user)}, demo_session_id)

    def _handle_login(self, storage: InMemoryStorage):
        """POST /users/login - Login user"""
        data = self._get_request_body()
        user_data = data.get("user", {})
        email = user_data.get("email")
        password = user_data.get("password")
        if not all([email, password]):
            self._send_error(422, {"errors": {"body": ["Email and password are required"]}})
            return
        user = get_user_by_email(email, storage)
        if not user or user["password"] != hash_password(password):
            self._send_error(401, {"errors": {"body": ["Invalid credentials"]}})
            return
        user["token"] = generate_token(user["id"])  # Generate new token
        demo_session_id = None if self._get_demo_session_cookie() else uuid.uuid4()
        self._send_response(200, {"user": create_user_response(user)}, demo_session_id)

    def _handle_get_current_user(self, storage: InMemoryStorage, current_user_id: Optional[int]):
        """GET /user - Get current user"""
        user_id = self._require_auth(current_user_id)
        user = storage.users.get(user_id)
        self._send_response(200, {"user": create_user_response(user)})

    def _helper_update_user_field(self, source_dict, target_dict, name, max_len):
        """returns True if there is an error"""
        if name not in source_dict:
            return False
        if type(source_dict[name]) != str or len(source_dict[name]) > max_len:
            err_str = f"{name} is an optional string of length <= {max_len}"
            self._send_error(422, {"errors": {"body": [err_str]}})
            return True
        target_dict[name] = source_dict[name]
        return False

    def _handle_update_user(self, storage: InMemoryStorage, current_user_id: Optional[int]):
        """PUT /user - Update current user"""
        user_id = self._require_auth(current_user_id)
        data = self._get_request_body()
        user_data = data.get("user", {})
        user_update = {}
        # Update fields if provided
        if (
            self._helper_update_user_field(user_data, user_update, "username", MAX_LEN_USER_USERNAME)
            or self._helper_update_user_field(user_data, user_update, "password", MAX_LEN_USER_PASSWORD)
            or self._helper_update_user_field(user_data, user_update, "bio", MAX_LEN_USER_BIO)
            or self._helper_update_user_field(user_data, user_update, "image", MAX_LEN_USER_IMAGE)
        ):
            return
        if "password" in user_update:
            user_update["password"] = hash_password(user_update["password"])
        user = storage.users.get(user_id)
        user.update(**user_update)
        self._send_response(200, {"user": create_user_response(user)})

    # Profile endpoints

    def _handle_get_profile(self, storage: InMemoryStorage, username: str, current_user_id: Optional[int]):
        """GET /profiles/{username} - Get profile"""
        user = get_user_by_username(username, storage)
        if not user:
            self._send_error(404, {"errors": {"body": ["Profile not found"]}})
            return
        self._send_response(200, {"profile": create_profile_response(user, storage, current_user_id)})

    def _handle_follow_user(self, storage: InMemoryStorage, username: str, current_user_id: Optional[int]):
        """POST /profiles/{username}/follow - Follow user"""
        user_id = self._require_auth(current_user_id)
        target_user = get_user_by_username(username, storage)
        if not target_user:
            self._send_error(404, {"errors": {"body": ["Profile not found"]}})
            return
        if target_user["id"] == user_id:
            self._send_error(422, {"errors": {"body": ["Cannot follow yourself"]}})
            return
        storage.follows.add(user_id, target_user["id"])
        self._send_response(200, {"profile": create_profile_response(target_user, storage, user_id)})

    def _handle_unfollow_user(self, storage: InMemoryStorage, username: str, current_user_id: Optional[int]):
        """DELETE /profiles/{username}/follow - Unfollow user"""
        user_id = self._require_auth(current_user_id)
        target_user = get_user_by_username(username, storage)
        if not target_user:
            self._send_error(404, {"errors": {"body": ["Profile not found"]}})
            return
        storage.follows.remove(user_id, target_user["id"])
        self._send_response(200, {"profile": create_profile_response(target_user, storage, user_id)})

    # Article endpoints

    def _handle_list_articles(self, storage: InMemoryStorage, query_params: Dict, current_user_id: Optional[int]):
        """GET /articles - List articles"""
        tag = query_params.get("tag", [None])[0]
        author = query_params.get("author", [None])[0]
        favorited = query_params.get("favorited", [None])[0]
        limit = int(query_params.get("limit", [20])[0])
        offset = int(query_params.get("offset", [0])[0])
        articles = list(storage.articles.values())
        # Filter by tag
        if tag:
            articles = [a for a in articles if tag in a["tagList"]]
        # Filter by author
        if author:
            author_user = get_user_by_username(author, storage)
            articles = [a for a in articles if a["author_id"] == author_user["id"]] if author_user else []
        # Filter by favorited
        if favorited:
            favorited_user = get_user_by_username(favorited, storage)
            if favorited_user:
                favorited_article_ids = storage.favorites.targets_for_source(favorited_user["id"])
                articles = [a for a in articles if a["id"] in favorited_article_ids]
            else:
                articles = []
        # Sort by creation date (newest first)
        articles.sort(key=lambda x: x["createdAt"], reverse=True)
        # Apply pagination
        total_count = len(articles)
        articles = articles[offset : offset + limit]
        # Format response
        article_responses = [create_article_response(a, storage, current_user_id) for a in articles]
        self._send_response(200, {"articles": article_responses, "articlesCount": total_count})

    def _handle_articles_feed(self, storage: InMemoryStorage, query_params: Dict, current_user_id: Optional[str]):
        """GET /articles/feed - Get feed of followed users"""
        user_id = self._require_auth(current_user_id)
        limit = int(query_params.get("limit", [20])[0])
        offset = int(query_params.get("offset", [0])[0])
        followed_user_ids = storage.follows.targets_for_source(user_id)
        articles = [a for a in storage.articles.values() if a["author_id"] in followed_user_ids]
        # Sort by creation date (newest first)
        articles.sort(key=lambda x: x["createdAt"], reverse=True)
        # Apply pagination
        total_count = len(articles)
        articles = articles[offset : offset + limit]
        # Format response
        article_responses = [create_article_response(a, storage, current_user_id) for a in articles]
        self._send_response(200, {"articles": article_responses, "articlesCount": total_count})

    def _helper_article_get_slug(self, storage, title):
        """ensure slug is unique"""
        slug = generate_slug(title)
        base_slug = slug
        counter = 1
        while get_article_by_slug(slug, storage):
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    def _helper_article_field(self, source_dict, name, max_len):
        """returns True if there is an error"""
        if name not in source_dict:
            return False
        if type(source_dict[name]) != str or len(source_dict[name]) > max_len:
            err_str = f"{name} is an optional string of length <= {max_len}"
            self._send_error(422, {"errors": {"body": [err_str]}})
            return True
        return False

    def _handle_create_article(self, storage: InMemoryStorage, current_user_id: Optional[str]):
        """POST /articles - Create article"""
        user_id = self._require_auth(current_user_id)
        data = self._get_request_body()
        article_data = data.get("article", {})
        title = article_data.get("title")
        description = article_data.get("description")
        body = article_data.get("body")
        if not all([title, description, body]):
            self._send_error(422, {"errors": {"body": ["Title, description and body are required"]}})
            return
        if (
            self._helper_article_field(article_data, "title", MAX_LEN_ARTICLE_TITLE)
            or self._helper_article_field(article_data, "description", MAX_LEN_ARTICLE_DESCRIPTION)
            or self._helper_article_field(article_data, "body", MAX_LEN_ARTICLE_BODY)
        ):
            return
        tag_list = article_data.get("tagList", [])
        if (
            type(tag_list) != list
            or len(tag_list) > MAX_LEN_ARTICLE_TAG_LIST
            or any(type(e) != str for e in tag_list)
            or any(len(e) > MAX_LEN_ARTICLE_TAG_LEN for e in tag_list)
        ):
            err_str = f"tagList is an optional list of less than {MAX_LEN_ARTICLE_TAG_LIST} strings "
            err_str += f"of less than {MAX_LEN_ARTICLE_TAG_LEN} chars"
            self._send_error(422, {"errors": {"body": [err_str]}})
            return True
        slug = self._helper_article_get_slug(storage, title)
        # Create article
        current_time = get_current_time()
        article = {
            "slug": slug,
            "title": title,
            "description": description,
            "body": body,
            "tagList": sorted(tag_list),
            "author_id": user_id,
            "createdAt": current_time,
            "updatedAt": current_time,
        }
        storage.articles.add(article)
        self._send_response(201, {"article": create_article_response(article, storage, user_id)})

    def _handle_get_article(self, storage: InMemoryStorage, slug: str, current_user_id: Optional[str]):
        """GET /articles/{slug} - Get article"""
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        self._send_response(200, {"article": create_article_response(article, storage, current_user_id)})

    def _handle_update_article(self, storage: InMemoryStorage, slug: str, current_user_id: Optional[str]):
        """PUT /articles/{slug} - Update article"""
        user_id = self._require_auth(current_user_id)
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        if article["author_id"] != user_id:
            self._send_error(403, {"errors": {"body": ["Forbidden"]}})
            return
        data = self._get_request_body()
        article_data = data.get("article", {})
        article_update = {}  # update this intermediary dict to prevent half-finished updates
        # Update fields if provided
        if "title" in article_data and article["title"] != article_data["title"]:  # additional check for slug
            if (self._helper_article_field(article_data, "title", MAX_LEN_ARTICLE_TITLE)):
                return
            article_update["title"] = article_data["title"]
            article_update["slug"] = self._helper_article_get_slug(storage, title)  # checked different title before
        if "description" in article_data:
            if (self._helper_article_field(article_data, "description", MAX_LEN_ARTICLE_DESCRIPTION)):
                return
            article_update["description"] = article_data["description"]
        if "body" in article_data:
            if (self._helper_article_field(article_data, "body", MAX_LEN_ARTICLE_BODY)):
                return
            article_update["body"] = article_data["body"]
        article_update["updatedAt"] = get_current_time()
        article.update(**article_update)
        self._send_response(200, {"article": create_article_response(article, storage, user_id)})

    def _handle_delete_article(self, storage: InMemoryStorage, slug: str, current_user_id: Optional[str]):
        """DELETE /articles/{slug} - Delete article"""
        user_id = self._require_auth(current_user_id)
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        if article["author_id"] != user_id:
            self._send_error(403, {"errors": {"body": ["Forbidden"]}})
            return
        # Delete article and related data
        article_id = article["id"]
        storage.articles.delete(article_id)
        # Remove from favorites
        storage.favorites.delete_target(article_id)
        # Delete comments
        comments_to_delete = [c_id for c_id, c in storage.comments.items() if c["article_id"] == article_id]
        for c_id in comments_to_delete:
            storage.comments.delete(c_id)
        # Send 204 No Content
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _handle_favorite_article(self, storage: InMemoryStorage, slug: str, current_user_id: Optional[str]):
        """POST /articles/{slug}/favorite - Favorite article"""
        user_id = self._require_auth(current_user_id)
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        storage.favorites.add(user_id, article["id"])
        self._send_response(200, {"article": create_article_response(article, storage, user_id)})

    def _handle_unfavorite_article(self, storage: InMemoryStorage, slug: str, current_user_id: Optional[str]):
        """DELETE /articles/{slug}/favorite - Unfavorite article"""
        user_id = self._require_auth(current_user_id)
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        storage.favorites.remove(user_id, article["id"])
        self._send_response(200, {"article": create_article_response(article, storage, user_id)})

    # Comment endpoints

    def _handle_get_comments(self, storage: InMemoryStorage, slug: str, current_user_id: Optional[str]):
        """GET /articles/{slug}/comments - Get comments"""
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        comments = [c for c in storage.comments.values() if c["article_id"] == article["id"]]
        comments.sort(key=lambda x: x["createdAt"], reverse=True)
        comment_responses = [create_comment_response(c, storage, current_user_id) for c in comments]
        self._send_response(200, {"comments": comment_responses})

    def _handle_create_comment(self, storage: InMemoryStorage, slug: str, current_user_id: Optional[str]):
        """POST /articles/{slug}/comments - Create comment"""
        user_id = self._require_auth(current_user_id)
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        data = self._get_request_body()
        comment_data = data.get("comment", {})
        body = comment_data.get("body")
        if not body:
            self._send_error(422, {"errors": {"body": ["Body is required"]}})
            return
        if type(body) != str or len(body) > MAX_LEN_COMMENT_BODY:
            self._send_error(422, {"errors": {"body": [f"Body is a string of less than {MAX_LEN_COMMENT_BODY} chars"]}})
            return
        # Create comment
        current_time = get_current_time()
        comment = {
            "body": body,
            "article_id": article["id"],
            "author_id": user_id,
            "createdAt": current_time,
            "updatedAt": current_time,
        }
        storage.comments.add(comment)
        self._send_response(200, {"comment": create_comment_response(comment, storage, user_id)})

    def _handle_delete_comment(
        self, storage: InMemoryStorage, slug: str, comment_id: int, current_user_id: Optional[str]
    ):
        """DELETE /articles/{slug}/comments/{id} - Delete comment"""
        user_id = self._require_auth(current_user_id)
        article = get_article_by_slug(slug, storage)
        if not article:
            self._send_error(404, {"errors": {"body": ["Article not found"]}})
            return
        comment = storage.comments.get(comment_id)
        if not comment or comment["article_id"] != article["id"]:
            self._send_error(404, {"errors": {"body": ["Comment not found"]}})
            return
        # Only comment author or article author can delete
        if comment["author_id"] != user_id and article["author_id"] != user_id:
            self._send_error(403, {"errors": {"body": ["Forbidden"]}})
            return
        storage.comments.delete(comment_id)
        # Send 204 No Content
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # Tag endpoints

    def _handle_get_tags(self, storage: InMemoryStorage):
        """GET /tags - Get all tags"""
        self._send_response(200, {"tags": sorted({t for a in storage.articles.values() for t in a.get("tagList", [])})})

    def do_OPTIONS(self):
        """Handle OPTIONS requests for CORS"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


def run_server(port: int = 8000):
    """Run the RealWorld API server"""
    server_address = ("", port)
    httpd = HTTPServer(server_address, RealWorldHandler)  # ty: ignore[invalid-argument-type]

    print(f"RealWorld API Server running on http://localhost:{port}")
    print("API endpoints available:")
    print("  POST   /users  -------------------------- Register")
    print("  POST   /users/login  -------------------- Login")
    print("  GET    /user  --------------------------- Current user")
    print("  PUT    /user  --------------------------- Update user")
    print("  GET    /profiles/{username}  ------------ Get profile")
    print("  POST   /profiles/{username}/follow  ----- Follow user")
    print("  DELETE /profiles/{username}/follow  ----- Unfollow user")
    print("  GET    /articles  ----------------------- List articles")
    print("  GET    /articles/feed  ------------------ Get feed")
    print("  POST   /articles  ----------------------- Create article")
    print("  GET    /articles/{slug}  ---------------- Get article")
    print("  PUT    /articles/{slug}  ---------------- Update article")
    print("  DELETE /articles/{slug}  ---------------- Delete article")
    print("  POST   /articles/{slug}/favorite  ------- Favorite article")
    print("  DELETE /articles/{slug}/favorite  ------- Unfavorite article")
    print("  GET    /articles/{slug}/comments  ------- Get comments")
    print("  POST   /articles/{slug}/comments  ------- Create comment")
    print("  DELETE /articles/{slug}/comments/{id}  -- Delete comment")
    print("  GET    /tags  --------------------------- Get tags")
    print("\nPress Ctrl+C to stop the server")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.shutdown()


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    run_server(port)


#### TESTS #############################################################################################################


class TestInMemoryModel(TestCase):

    def setUp(self):
        self.model = InMemoryModel(max_count=3)

    # init

    def test_init(self):
        model = InMemoryModel(max_count=5)
        self.assertEqual(model.max_count, 5)
        self.assertEqual(model.objects, {})
        self.assertEqual(model.last_accessed_ids, [])
        self.assertEqual(model.current_id_counter, 1)

    def test_negative_max_count(self):
        with self.assertRaises(ValueError) as exc:
            model = InMemoryModel(max_count=-1)
        self.assertEqual(str(exc.exception), "invalid value for max_count")

    # add

    def test_add_single_object(self):
        obj = {"name": "test"}
        result = self.model.add(obj)
        self.assertEqual(result, obj)
        self.assertIn("id", obj)
        self.assertEqual(obj["id"], "1")
        self.assertEqual(self.model.current_id_counter, 2)
        self.assertEqual(len(self.model.objects), 1)
        self.assertIn("1", self.model.objects)

    def test_add_multiple_objects(self):
        obj1 = {"name": "test1"}
        obj2 = {"name": "test2"}
        obj3 = {"name": "test3"}
        self.model.add(obj1)
        self.model.add(obj2)
        self.model.add(obj3)
        self.assertEqual(obj1["id"], "1")
        self.assertEqual(obj2["id"], "2")
        self.assertEqual(obj3["id"], "3")
        self.assertEqual(self.model.current_id_counter, 4)
        self.assertEqual(len(self.model.objects), 3)

    def test_add_with_auto_id(self):
        """an object with a set is id is then expected"""
        model = InMemoryModel(max_count=3)
        obj = {"name": "test"}
        result = model.add(obj)
        self.assertEqual(result, obj)
        self.assertEqual(obj["id"], "1")
        self.assertEqual(model.current_id_counter, 2)

    def test_add_exceeds_max_count_will_overwrite(self):
        obj1 = {"name": "test1"}
        obj2 = {"name": "test2"}
        obj3 = {"name": "test3"}
        obj4 = {"name": "test4"}
        self.model.add(obj1)
        self.model.add(obj2)
        self.model.add(obj3)
        self.model.add(obj4)
        self.assertEqual(
            self.model.objects,
            {'2': {'name': 'test2', 'id': '2'}, '3': {'name': 'test3', 'id': '3'}, '4': {'name': 'test4', 'id': '4'}},
        )
        self.assertEqual(self.model.last_accessed_ids, ['2', '3', '4'])

    def test_add_dict_with_existing_id_key(self):
        # Test adding object that already has an "id" key
        obj = {"name": "test", "id": "existing_id"}
        self.model.add(obj)
        # Should overwrite the existing id
        self.assertEqual(obj["id"], "1")
        self.assertNotEqual(obj["id"], "existing_id")

    def test_current_id_counter_increments(self):
        initial_counter = self.model.current_id_counter
        self.model.add({"name": "test1"})
        self.assertEqual(self.model.current_id_counter, initial_counter + 1)
        self.model.add({"name": "test2"})
        self.assertEqual(self.model.current_id_counter, initial_counter + 2)

    # get

    def test_get_existing_object(self):
        obj = {"name": "test"}
        self.model.add(obj)
        retrieved = self.model.get("1")
        self.assertEqual(retrieved, obj)

    def test_get_nonexistent_object(self):
        result = self.model.get("999")
        self.assertIsNone(result)

    def test_get_with_int_id(self):
        obj = {"name": "test"}
        self.model.add(obj)
        retrieved = self.model.get(1)
        self.assertEqual(retrieved, obj)

    def test_get_with_invalid_id_type(self):
        with self.assertRaises(ValueError) as context:
            self.model.get({"invalid": "id"})
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_get_with_long_id(self):
        long_id = "x" * (MAX_ID_LEN + 1)
        with self.assertRaises(ValueError) as context:
            self.model.get(long_id)
        self.assertIn("id is too long", str(context.exception))

    # keys / values / items

    def test_keys(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        keys = list(self.model.keys())
        self.assertEqual(sorted(keys), ["1", "2"])

    def test_values(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.assertEqual(list(self.model.values()), [{"id": "1", "name": "test1"}, {"id": "2", "name": "test2"}])

    def test_items(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        items = dict(self.model.items())
        self.assertEqual(items, {"1": {"id": "1", "name": "test1"}, "2": {"id": "2", "name": "test2"}})

    # delete

    def test_delete_existing_object_with_one_object(self):
        obj = {"name": "test"}
        self.model.add(obj)
        result = self.model.delete("1")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 0)

    def test_delete_existing_object_with_multiple_objects_deletes_first(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.model.add({"name": "test3"})
        self.assertEqual(self.model.current_id_counter, 4)
        result = self.model.delete("1")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 2)
        self.assertEqual(self.model.objects, {'2': {'name': 'test2', 'id': '2'}, '3': {'name': 'test3', 'id': '3'}})
        self.assertEqual(self.model.last_accessed_ids, ['2', '3'])

    def test_delete_existing_object_with_multiple_objects_deletes_middle(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.model.add({"name": "test3"})
        self.assertEqual(self.model.current_id_counter, 4)
        result = self.model.delete("2")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 2)
        self.assertEqual(self.model.objects, {'1': {'name': 'test1', 'id': '1'}, '3': {'name': 'test3', 'id': '3'}})
        self.assertEqual(self.model.last_accessed_ids, ['1', '3'])

    def test_delete_existing_object_with_multiple_objects_deletes_last(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.model.add({"name": "test3"})
        self.assertEqual(self.model.current_id_counter, 4)
        result = self.model.delete("3")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 2)
        self.assertEqual(self.model.objects, {'1': {'name': 'test1', 'id': '1'}, '2': {'name': 'test2', 'id': '2'}})
        self.assertEqual(self.model.last_accessed_ids, ['1', '2'])

    def test_delete_nonexistent_object(self):
        result = self.model.delete("999")
        self.assertFalse(result)

    def test_delete_with_int_id(self):
        obj = {"name": "test"}
        self.model.add(obj)
        result = self.model.delete(1)
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 0)

    def test_delete_with_invalid_id_type(self):
        with self.assertRaises(ValueError) as context:
            self.model.delete({"invalid": "id"})
        self.assertIn("id must be an int or an str", str(context.exception))

    # mixed

    def test_max_id_length_exceeded(self):
        # Create a model with a very low MAX_ID_LEN to test the limit
        original_max_id_len = MAX_ID_LEN
        import realworld_dummy_server
        realworld_dummy_server.MAX_ID_LEN = 1
        try:
            model = InMemoryModel(max_count=10)
            # Add enough objects to reach the limit
            for i in range(9):  # IDs will be 1-9 (single digit)
                model.add({"name": f"test{i}"})
            # Adding the 10th object should trigger the error (ID would be "10", length 2)
            with self.assertRaises(ValueError) as context:
                model.add({"name": "test10"})
            self.assertIn("cannot allocate id: we reached MAX_ID_LEN limit", str(context.exception))
        finally:
            # Restore original value
            realworld_dummy_server.MAX_ID_LEN = original_max_id_len

    def test_empty_model_operations(self):
        # Test operations on empty model
        self.assertEqual(len(list(self.model.keys())), 0)
        self.assertEqual(len(list(self.model.values())), 0)
        self.assertEqual(len(list(self.model.items())), 0)
        self.assertIsNone(self.model.get("1"))
        self.assertFalse(self.model.delete("1"))

    def test_object_references_maintained(self):
        # Test that the same object reference is returned
        obj = {"name": "test", "data": [1, 2, 3]}
        self.model.add(obj)
        retrieved = self.model.get("1")
        self.assertIs(retrieved, obj)
        # Modify the original object
        obj["name"] = "modified"
        retrieved_again = self.model.get("1")
        self.assertEqual(retrieved_again["name"], "modified")

    def test_model_zero_max_count(self):
        with self.assertRaises(ValueError) as exc:
            model = InMemoryModel(max_count=0)
        self.assertEqual(str(exc.exception), "invalid value for max_count")

    def test_one_max_count(self):
        model = InMemoryModel(max_count=1)
        obj1 = {"name": "test1"}
        obj2 = {"name": "test2"}
        model.add(obj1)
        self.assertEqual(len(model.objects), 1)
        model.add(obj2)
        self.assertEqual(len(model.objects), 1)
        self.assertIsNone(model.get("1"))
        remaining_obj = model.get("2")
        self.assertEqual(remaining_obj, obj2)

    def test_string_and_int_id_equivalence(self):
        self.model.add({"name": "test"})
        self.assertEqual(self.model.get("1"), {"id": "1", "name": "test"})
        self.assertEqual(self.model.get(1), {"id": "1", "name": "test"})

    def test_large_max_count(self):
        model = InMemoryModel(max_count=1000000)
        obj = {"name": "test"}
        model.add(obj)
        self.assertEqual(len(model.objects), 1)
        self.assertEqual(obj["id"], "1")

    def test_float_ids_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get(1.5)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_boolean_ids_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get(True)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_none_id_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get(None)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_list_id_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get([1, 2, 3])
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_empty_string_id(self):
        obj = {"name": "test"}
        self.model.add(obj)
        # Empty string should be valid
        result = self.model.get("")
        self.assertIsNone(result)  # Won't match "1"

    def test_zero_string_id(self):
        result = self.model.get("0")
        self.assertIsNone(result)

    def test_zero_int_id(self):
        result = self.model.get(0)
        self.assertIsNone(result)

    def test_negative_int_id(self):
        result = self.model.get(-1)
        self.assertIsNone(result)

    def test_very_large_int_id(self):
        import sys
        very_large_id = sys.maxsize
        result = self.model.get(very_large_id)
        self.assertIsNone(result)

    def test_unicode_string_id(self):
        unicode_id = "测试"
        result = self.model.get(unicode_id)
        self.assertIsNone(result)

    def test_max_id_len_boundary(self):
        boundary_id = "x" * MAX_ID_LEN
        result = self.model.get(boundary_id)
        self.assertIsNone(result)

    def test_model_state_after_failed_operations(self):
        # Test that model state remains consistent after failed operations
        obj1 = {"name": "test1"}
        self.model.add(obj1)
        initial_objects_count = len(self.model.objects)
        initial_counter = self.model.current_id_counter
        # Try invalid operations
        try:
            self.model.get({"invalid": "id"})
        except ValueError:
            pass
        try:
            self.model.delete({"invalid": "id"})
        except ValueError:
            pass
        # State should be unchanged
        self.assertEqual(len(self.model.objects), initial_objects_count)
        self.assertEqual(self.model.current_id_counter, initial_counter)

    def test_concurrent_access_simulation(self):
        # Simulate concurrent access patterns
        objects = []
        for i in range(5):
            obj = {"name": f"test{i}", "value": i * 10}
            self.model.add(obj)
            objects.append(obj)
        # Simulate multiple readers
        for i in range(0, 2):
            retrieved = self.model.get(str(i + 1))
            self.assertIsNone(retrieved)
        for i in range(2, 5):
            retrieved = self.model.get(str(i + 1))
            self.assertEqual(retrieved["name"], f"test{i}")
            self.assertEqual(retrieved["value"], i * 10)
        # Verify all objects still accessible
        self.assertEqual(len(list(self.model.keys())), 3)  # max_count is 3


class TestInMemoryLinks(TestCase):

    def setUp(self):
        self.links = InMemoryLinks(max_count=3)

    def test_init(self):
        links = InMemoryLinks(max_count=5)
        self.assertEqual(links.max_count, 5)
        self.assertEqual(links.links, [])

    def test_add_single_link(self):
        self.links.add("1", "2")
        self.assertEqual(self.links.links, [("1", "2")])

    def test_add_multiple_links(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.assertEqual(self.links.links, [("1", "2"), ("2", "3"), ("3", "4")])

    def test_add_duplicate_link_moves_to_end(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("1", "2")  # Duplicate
        self.assertEqual(self.links.links, [("2", "3"), ("1", "2")])

    def test_add_exceeds_max_count_removes_oldest(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.links.add("4", "5")  # Should remove ("1", "2")
        self.assertEqual(self.links.links, [("2", "3"), ("3", "4"), ("4", "5")])

    def test_add_duplicate_when_at_max_count(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.links.add("2", "3")  # Duplicate when at max
        self.assertEqual(self.links.links, [("1", "2"), ("3", "4"), ("2", "3")])

    def test_remove_existing_link(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.remove("1", "2")
        self.assertEqual(self.links.links, [("2", "3")])

    def test_remove_nonexistent_link(self):
        self.links.add("1", "2")
        self.links.remove("3", "4")  # Doesn't exist
        self.assertEqual(self.links.links, [("1", "2")])

    def test_remove_from_empty_links(self):
        self.links.remove("1", "2")
        self.assertEqual(self.links.links, [])

    def test_remove_middle_link(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.links.remove("2", "3")
        self.assertEqual(self.links.links, [("1", "2"), ("3", "4")])

    def test_link_zero_max_count(self):
        links = InMemoryLinks(max_count=0)
        links.add("1", "2")
        self.assertEqual(links.links, [])

    def test_one_max_count(self):
        links = InMemoryLinks(max_count=1)
        links.add("1", "2")
        links.add("2", "3")
        self.assertEqual(links.links, [("2", "3")])

    def test_mixed_operations(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.remove("1", "2")
        self.links.add("3", "4")
        self.links.add("4", "5")
        self.assertEqual(self.links.links, [("2", "3"), ("3", "4"), ("4", "5")])

    def test_add_same_link_multiple_times(self):
        self.links.add("1", "2")
        self.links.add("1", "2")
        self.links.add("1", "2")
        self.assertEqual(self.links.links, [("1", "2")])

    def test_edge_case_same_source_and_target(self):
        self.links.add("1", "1")
        self.assertEqual(self.links.links, [("1", "1")])
        self.links.remove("1", "1")
        self.assertEqual(self.links.links, [])

    def test_add_int_converts_to_str(self):
        self.links.add(1, 2)
        self.assertEqual(self.links.links, [("1", "2")])

    def test_add_boolean_raises_error(self):
        with self.assertRaises(ValueError) as context:
            self.links.add(True, False)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_add_dict_raises_error(self):
        with self.assertRaises(ValueError) as context:
            self.links.add({"key": "value"}, {"other": "data"})
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_is_linked_empty_links(self):
        self.assertFalse(self.links.is_linked("1", "2"))

    def test_is_linked_existing_link(self):
        self.links.add("1", "2")
        self.assertTrue(self.links.is_linked("1", "2"))

    def test_is_linked_nonexistent_link(self):
        self.links.add("1", "2")
        self.assertFalse(self.links.is_linked("2", "1"))
        self.assertFalse(self.links.is_linked("1", "3"))

    def test_is_linked_with_int_ids(self):
        self.links.add(1, 2)
        self.assertTrue(self.links.is_linked(1, 2))
        self.assertTrue(self.links.is_linked("1", "2"))

    def test_is_linked_after_removal(self):
        self.links.add("1", "2")
        self.assertTrue(self.links.is_linked("1", "2"))
        self.links.remove("1", "2")
        self.assertFalse(self.links.is_linked("1", "2"))

    def test_targets_for_source_empty_links(self):
        self.assertEqual(self.links.targets_for_source("1"), [])

    def test_targets_for_source_no_matches(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.assertEqual(self.links.targets_for_source("3"), [])

    def test_targets_for_source_single_target(self):
        self.links.add("1", "2")
        self.assertEqual(self.links.targets_for_source("1"), ["2"])

    def test_targets_for_source_multiple_targets(self):
        self.links.add("1", "2")
        self.links.add("1", "3")
        self.links.add("1", "4")
        targets = self.links.targets_for_source("1")
        self.assertEqual(sorted(targets), ["2", "3", "4"])

    def test_targets_for_source_mixed_sources(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("1", "4")
        self.assertEqual(sorted(self.links.targets_for_source("1")), ["2", "4"])
        self.assertEqual(self.links.targets_for_source("2"), ["3"])

    def test_targets_for_source_with_int_id(self):
        self.links.add(1, 2)
        self.links.add(1, 3)
        targets = self.links.targets_for_source(1)
        self.assertEqual(sorted(targets), ["2", "3"])

    def test_sources_for_target_empty_links(self):
        self.assertEqual(self.links.sources_for_target("1"), [])

    def test_sources_for_target_no_matches(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.assertEqual(self.links.sources_for_target("1"), [])

    def test_sources_for_target_single_source(self):
        self.links.add("1", "2")
        self.assertEqual(self.links.sources_for_target("2"), ["1"])

    def test_sources_for_target_multiple_sources(self):
        self.links.add("1", "4")
        self.links.add("2", "4")
        self.links.add("3", "4")
        sources = self.links.sources_for_target("4")
        self.assertEqual(sorted(sources), ["1", "2", "3"])

    def test_sources_for_target_mixed_targets(self):
        self.links.add("1", "2")
        self.links.add("3", "2")
        self.links.add("1", "4")
        self.assertEqual(sorted(self.links.sources_for_target("2")), ["1", "3"])
        self.assertEqual(self.links.sources_for_target("4"), ["1"])

    def test_sources_for_target_with_int_id(self):
        self.links.add(1, 3)
        self.links.add(2, 3)
        sources = self.links.sources_for_target(3)
        self.assertEqual(sorted(sources), ["1", "2"])

    def test_delete_source_empty_links(self):
        self.links.delete_source("1")
        self.assertEqual(self.links.links, [])

    def test_delete_source_single_match(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        self.links.delete_source("1")
        self.assertEqual(self.links.links, [("3", "4")])

    def test_delete_source_multiple_matches(self):
        self.links.add("1", "2")
        self.links.add("1", "3")
        self.links.add("2", "4")
        self.links.delete_source("1")
        self.assertEqual(self.links.links, [("2", "4")])

    def test_delete_source_no_matches(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        original_links = self.links.links[:]
        self.links.delete_source("5")
        self.assertEqual(self.links.links, original_links)

    def test_delete_source_with_int_id(self):
        self.links.add(1, 2)
        self.links.add(3, 4)
        self.links.delete_source(1)
        self.assertEqual(self.links.links, [("3", "4")])

    def test_delete_target_empty_links(self):
        self.links.delete_target("1")
        self.assertEqual(self.links.links, [])

    def test_delete_target_single_match(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        self.links.delete_target("2")
        self.assertEqual(self.links.links, [("3", "4")])

    def test_delete_target_multiple_matches(self):
        self.links.add("1", "4")
        self.links.add("2", "4")
        self.links.add("3", "5")
        self.links.delete_target("4")
        self.assertEqual(self.links.links, [("3", "5")])

    def test_delete_target_no_matches(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        original_links = self.links.links[:]
        self.links.delete_target("5")
        self.assertEqual(self.links.links, original_links)

    def test_delete_target_with_int_id(self):
        self.links.add(1, 2)
        self.links.add(3, 4)
        self.links.delete_target(2)
        self.assertEqual(self.links.links, [("3", "4")])


class TestStorageContainer(TestCase):

    # Setup

    def setUp(self):
        self.container = _StorageContainer(disable_isolation_mode=False)

    # Helpers

    def _verify_heap_property(self, container):
        # Helper to verify min-heap property for a given container
        for i in range(len(container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(container.heap):
                self.assertLessEqual(
                    container.heap[i][0],
                    container.heap[left_child][0],
                    f"Heap property violated at index {i} and left child {left_child}"
                )
            if right_child < len(container.heap):
                self.assertLessEqual(
                    container.heap[i][0],
                    container.heap[right_child][0],
                    f"Heap property violated at index {i} and right child {right_child}"
                )

    def _verify_index_consistency(self, container):
        # Helper to verify index_map consistency with heap for a given container
        self.assertEqual(len(container.index_map), len(container.heap))
        for item_id, index in container.index_map.items():
            # Index should be valid
            self.assertGreaterEqual(index, 0)
            self.assertLess(index, len(container.heap))
            # Heap item at index should match
            heap_item = container.heap[index]
            self.assertEqual(heap_item[1], item_id, f"Index map inconsistency for {item_id}")
            self.assertEqual(heap_item[3], index, f"Internal index inconsistency for {item_id}")
        # Every heap item should be in index_map
        for i, heap_item in enumerate(container.heap):
            item_id = heap_item[1]
            self.assertIn(item_id, container.index_map)
            self.assertEqual(container.index_map[item_id], i)

    # Tests

    def test_heap_push_single_item(self):
        self.container._push(5, "item1", "data1")
        self.assertEqual(len(self.container.heap), 1)
        self.assertEqual(self.container.heap[0], [5, "item1", "data1", 0])
        self.assertEqual(self.container.index_map["item1"], 0)

    def test_heap_push_multiple_items_maintains_min_heap(self):
        self.container._push(10, "item1", "data1")
        self.container._push(5, "item2", "data2")
        self.container._push(15, "item3", "data3")
        self.container._push(3, "item4", "data4")
        # Root should be the minimum
        self.assertEqual(self.container.heap[0][0], 3)
        self.assertEqual(self.container.heap[0][1], "item4")
        # Verify heap property: parent <= children
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_heap_pop_empty_heap(self):
        result = self.container._pop()
        self.assertIsNone(result)

    def test_heap_pop_single_item(self):
        self.container._push(5, "item1", "data1")
        result = self.container._pop()
        self.assertEqual(result, [5, "item1", "data1", 0])
        self.assertEqual(len(self.container.heap), 0)
        self.assertNotIn("item1", self.container.index_map)

    def test_heap_pop_multiple_items_returns_min(self):
        items = [(10, "item1"), (5, "item2"), (15, "item3"), (3, "item4"), (7, "item5")]
        for priority, item_id in items:
            self.container._push(priority, item_id, f"data_{item_id}")
        # Pop items should come out in priority order
        result1 = self.container._pop()
        self.assertEqual(result1[0], 3)  # minimum priority
        self.assertEqual(result1[1], "item4")
        result2 = self.container._pop()
        self.assertEqual(result2[0], 5)
        self.assertEqual(result2[1], "item2")
        # Verify heap property is maintained after pops
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_update_priority_increase(self):
        self.container._push(5, "item1", "data1")
        self.container._push(10, "item2", "data2")
        self.container._push(15, "item3", "data3")
        # Increase priority of root element
        self.container._update_priority("item1", 20)
        # Root should no longer be item1
        self.assertNotEqual(self.container.heap[0][1], "item1")
        # Verify heap property is maintained
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_update_priority_decrease(self):
        self.container._push(15, "item1", "data1")
        self.container._push(10, "item2", "data2")
        self.container._push(20, "item3", "data3")
        # Decrease priority of last element to make it root
        self.container._update_priority("item3", 1)
        # Root should now be item3
        self.assertEqual(self.container.heap[0][1], "item3")
        self.assertEqual(self.container.heap[0][0], 1)

    def test_update_priority_nonexistent_item(self):
        self.container._push(5, "item1", "data1")
        with self.assertRaises(ValueError) as context:
            self.container._update_priority("nonexistent", 10)
        self.assertIn("not found in heap", str(context.exception))

    def test_get_storage_with_isolation_disabled(self):
        container = _StorageContainer(disable_isolation_mode=True)
        storage0 = container.get_storage(None)
        storage1 = container.get_storage("session1")
        storage2 = container.get_storage("session2")
        # All should return the same storage
        self.assertIs(storage1, storage0)
        self.assertIs(storage2, storage0)

    def test_get_storage_with_isolation_enabled_2_different_session(self):
        container = _StorageContainer(disable_isolation_mode=False)
        storage1 = container.get_storage("session1")
        storage2 = container.get_storage("session2")
        # Different sessions should get different storage
        self.assertIsNot(storage1, storage2)

    def test_get_storage_with_isolation_enabled_2_same(self):
        container = _StorageContainer(disable_isolation_mode=False)
        storage1 = container.get_storage("session1")
        container.get_storage("something-else")  # Call to other session in between
        storage1_bis = container.get_storage("session1")
        # Storage containers from the same id should get the same storage
        self.assertIs(storage1, storage1_bis)

    def test_get_storage_with_isolation_enabled_2_default_sessions_are_not_the_same_none_version(self):
        """We don't want the modifications of a default session to have an impact for other users"""
        container = _StorageContainer(disable_isolation_mode=False)
        storage1 = container.get_storage(None)
        storage2 = container.get_storage(None)
        # Multiple defaults sessions should get different storage
        self.assertIsNot(storage1, storage2)

    def test_get_storage_with_isolation_enabled_2_default_sessions_are_not_the_same_empty_string_version(self):
        """We don't want the modifications of a default session to have an impact for other users"""
        container = _StorageContainer(disable_isolation_mode=False)
        storage1 = container.get_storage("")
        storage2 = container.get_storage("")
        # Multiple defaults sessions should get different storage
        self.assertIsNot(storage1, storage2)

    def test_heap_index_consistency(self):
        # Test that index_map stays consistent with heap positions
        items = [(10, "a"), (5, "b"), (15, "c"), (3, "d"), (7, "e"), (12, "f")]
        for priority, item_id in items:
            self.container._push(priority, item_id, f"data_{item_id}")
        # Verify all items are in index_map
        for _, item_id in items:
            self.assertIn(item_id, self.container.index_map)
        # Verify index_map points to correct positions
        for item_id, index in self.container.index_map.items():
            self.assertEqual(self.container.heap[index][1], item_id)
            self.assertEqual(self.container.heap[index][3], index)
        # Pop some items and verify consistency
        self.container._pop()
        self.container._pop()
        # Re-verify consistency
        for item_id, index in self.container.index_map.items():
            self.assertEqual(self.container.heap[index][1], item_id)
            self.assertEqual(self.container.heap[index][3], index)

    def test_sift_operations_maintain_heap_property(self):
        # Test internal sift operations
        self.container.heap = [[10, "a", "data_a", 0], [5, "b", "data_b", 1], [15, "c", "data_c", 2]]
        self.container.index_map = {"a": 0, "b": 1, "c": 2}
        # Manually trigger sift_up (simulating priority decrease)
        self.container.heap[2][0] = 1  # Change priority
        self.container._sift_up(2)
        # Verify heap property
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_swap_operation(self):
        self.container._push(10, "item1", "data1")
        self.container._push(5, "item2", "data2")
        # Test swap operation
        orig_item1_pos = self.container.index_map["item1"]
        orig_item2_pos = self.container.index_map["item2"]
        self.container._swap(0, 1)
        # Verify positions swapped
        self.assertEqual(self.container.index_map["item1"], orig_item2_pos)
        self.assertEqual(self.container.index_map["item2"], orig_item1_pos)
        # Verify heap items swapped
        self.assertEqual(self.container.heap[orig_item2_pos][1], "item1")
        self.assertEqual(self.container.heap[orig_item1_pos][1], "item2")
        # Verify internal indices updated
        self.assertEqual(self.container.heap[orig_item2_pos][3], orig_item2_pos)
        self.assertEqual(self.container.heap[orig_item1_pos][3], orig_item1_pos)

    def test_heap_with_duplicate_priorities(self):
        # Test heap behavior with duplicate priorities
        self.container._push(5, "item1", "data1")
        self.container._push(5, "item2", "data2")
        self.container._push(5, "item3", "data3")
        self.container._push(3, "item4", "data4")
        self.container._push(5, "item5", "data5")
        # Root should be minimum priority
        self.assertEqual(self.container.heap[0][0], 3)
        self.assertEqual(self.container.heap[0][1], "item4")
        # Verify heap property with duplicates
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Pop minimum and verify heap still valid
        result = self.container._pop()
        self.assertEqual(result[0], 3)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_update_priority_to_same_value(self):
        # Test updating priority to the same value (should be no-op)
        self.container._push(10, "item1", "data1")
        self.container._push(5, "item2", "data2")
        original_heap = [item[:] for item in self.container.heap]  # Deep copy
        original_index_map = self.container.index_map.copy()
        self.container._update_priority("item1", 10)  # Same priority
        # Heap should be unchanged
        self.assertEqual(len(self.container.heap), len(original_heap))
        self.assertEqual(self.container.index_map, original_index_map)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_pop_all_items_sequential(self):
        # Test popping all items from heap
        priorities = [15, 3, 8, 1, 12, 6, 20, 4]
        items = []
        for i, priority in enumerate(priorities):
            item_id = f"item{i}"
            self.container._push(priority, item_id, f"data{i}")
            items.append((priority, item_id))
        # Pop all items and verify they come out in sorted order
        popped_priorities = []
        while len(self.container.heap) > 0:
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)
            result = self.container._pop()
            popped_priorities.append(result[0])
        # Should be in ascending order
        self.assertEqual(popped_priorities, sorted([p for p, _ in items]))
        self.assertEqual(len(self.container.heap), 0)
        self.assertEqual(len(self.container.index_map), 0)

    def test_mixed_operations_consistency(self):
        # Test mix of push, pop, and update operations
        self.container._push(10, "a", "data_a")
        self.container._push(5, "b", "data_b")
        self.container._push(15, "c", "data_c")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Update priority
        self.container._update_priority("c", 1)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        self.assertEqual(self.container.heap[0][1], "c")  # Should be new root
        # Pop minimum
        result = self.container._pop()
        self.assertEqual(result[1], "c")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Add more items
        self.container._push(3, "d", "data_d")
        self.container._push(8, "e", "data_e")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Update existing item
        self.container._update_priority("b", 20)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_empty_heap_edge_cases(self):
        # Test operations on empty heap
        self.assertEqual(len(self.container.heap), 0)
        self.assertEqual(len(self.container.index_map), 0)
        # Pop from empty heap
        result = self.container._pop()
        self.assertIsNone(result)
        # Update non-existent item
        with self.assertRaises(ValueError):
            self.container._update_priority("nonexistent", 10)

    def test_large_heap_operations(self):
        # Stress test with many items
        import random
        random.seed(42)  # For reproducible tests
        items = []
        num_items = 100
        # Push many items
        for i in range(num_items):
            priority = random.randint(1, 1000)
            item_id = f"item_{i}"
            self.container._push(priority, item_id, f"data_{i}")
            items.append((priority, item_id))
            # Verify heap property periodically
            if i % 20 == 0:
                self._verify_heap_property(self.container)
                self._verify_index_consistency(self.container)
        # Final verification
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        self.assertEqual(len(self.container.heap), num_items)
        self.assertEqual(len(self.container.index_map), num_items)
        # Update random items
        for _ in range(20):
            item_idx = random.randint(0, num_items - 1)
            item_id = f"item_{item_idx}"
            new_priority = random.randint(1, 1000)
            self.container._update_priority(item_id, new_priority)
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)

    def test_boundary_priorities(self):
        # Test with extreme priority values
        import sys
        # Test with very large and small numbers
        self.container._push(sys.maxsize, "max_item", "max_data")
        self.container._push(-sys.maxsize, "min_item", "min_data")
        self.container._push(0, "zero_item", "zero_data")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Min should be at root
        self.assertEqual(self.container.heap[0][0], -sys.maxsize)
        self.assertEqual(self.container.heap[0][1], "min_item")
        # Pop and verify order
        result1 = self.container._pop()
        self.assertEqual(result1[0], -sys.maxsize)
        result2 = self.container._pop()
        self.assertEqual(result2[0], 0)
        result3 = self.container._pop()
        self.assertEqual(result3[0], sys.maxsize)

    def test_special_character_item_ids(self):
        # Test with various item ID formats
        special_ids = [
            "item-with-dashes",
            "item_with_underscores",
            "item.with.dots",
            "item with spaces",
            "item@with#symbols",
            "123numeric_start",
            "",  # empty string
            "🎯emoji_id",
            "very_long_" + "x" * 100 + "_id"
        ]
        for i, item_id in enumerate(special_ids):
            self.container._push(i + 1, item_id, f"data_{i}")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Update some items
        self.container._update_priority("item-with-dashes", 50)
        self.container._update_priority("🎯emoji_id", 0)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_heap_after_multiple_updates(self):
        # Test heap consistency after many priority updates
        items = ["a", "b", "c", "d", "e", "f"]
        priorities = [10, 20, 30, 40, 50, 60]
        for item_id, priority in zip(items, priorities):
            self.container._push(priority, item_id, f"data_{item_id}")
        # Perform multiple updates that should change heap structure
        updates = [
            ("f", 1),   # Move last to first
            ("a", 100), # Move first to last
            ("c", 15),  # Minor adjustment
            ("e", 5),   # Move middle to near front
        ]
        for item_id, new_priority in updates:
            self.container._update_priority(item_id, new_priority)
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)
        # Verify final order by popping all
        popped_items = []
        while self.container.heap:
            result = self.container._pop()
            popped_items.append((result[0], result[1]))
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)
        # Should be in priority order
        popped_priorities = [priority for priority, _ in popped_items]
        self.assertEqual(popped_priorities, sorted(popped_priorities))

    def test_sift_operations_edge_cases(self):
        # Test sift operations at heap boundaries
        # Single item - sift operations should be no-ops
        self.container._push(5, "single", "data")
        original_heap = [item[:] for item in self.container.heap]
        self.container._sift_up(0)
        self.container._sift_down(0)
        self.assertEqual(self.container.heap, original_heap)
        # Two items
        self.container._push(10, "second", "data2")
        self._verify_heap_property(self.container)
        # Manually test sift operations
        if self.container.heap[1][0] < self.container.heap[0][0]:
            self.container._swap(0, 1)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_max_sessions_is_working_with_a_continuous_sequence(self):
        # Test that when max_sessions is reached, oldest sessions are evicted
        max_sessions = 3
        container = _StorageContainer(disable_isolation_mode=False, max_sessions=max_sessions)
        # Create sessions up to the limit
        storages = []
        for i in range(max_sessions):
            session_id = f"session_{i}"
            storage = container.get_storage(session_id)
            storages.append(storage)
            # Verify storage was created
            self.assertIsNotNone(storage)
            # Verify it's in the container
            self.assertIn(session_id, container.index_map)
            # Verify heap properties after each insertion
            self._verify_heap_property(container)
            self._verify_index_consistency(container)
        # All sessions should be present
        self.assertEqual(len(container.index_map), max_sessions)
        self.assertEqual(len(container.heap), max_sessions)
        # Add one more session - should evict the oldest (first) session
        new_session_id = "session_new"
        new_storage = container.get_storage(new_session_id)
        # Verify heap properties after eviction and insertion
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Should still have max_sessions total
        self.assertEqual(len(container.index_map), max_sessions)
        self.assertEqual(len(container.heap), max_sessions)
        # New session should be present
        self.assertIn(new_session_id, container.index_map)
        # First session should have been evicted (it had the smallest timestamp)
        self.assertNotIn("session_0", container.index_map)

    def test_max_sessions_is_working_with_a_sequence_of_calls_actually_triggering_reorders(self):
        # Test that accessing existing sessions updates their priority and affects eviction order
        import time
        max_sessions = 3
        container = _StorageContainer(disable_isolation_mode=False, max_sessions=max_sessions)
        # Create initial sessions
        session_ids = ["session_1", "session_2", "session_3"]
        for session_id in session_ids:
            container.get_storage(session_id)
            time.sleep(0.00001)  # Small delay to ensure different timestamps
            # Verify heap properties after each insertion
            self._verify_heap_property(container)
            self._verify_index_consistency(container)
        # Access session_1 to update its priority (make it more recently used)
        time.sleep(0.00001)
        container.get_storage("session_1")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Add a new session - should evict session_2 (oldest untouched)
        time.sleep(0.00001)
        container.get_storage("session_4")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Verify session_1 and session_3 are still present (session_1 was recently accessed)
        self.assertIn("session_1", container.index_map)
        self.assertIn("session_3", container.index_map)
        self.assertIn("session_4", container.index_map)
        # session_2 should have been evicted (it was the oldest unused)
        self.assertNotIn("session_2", container.index_map)
        # Verify we still have exactly max_sessions
        self.assertEqual(len(container.index_map), max_sessions)
        self.assertEqual(len(container.heap), max_sessions)
        # Access session_3 multiple times to make it most recent
        time.sleep(0.00001)
        container.get_storage("session_3")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        time.sleep(0.00001)
        container.get_storage("session_3")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Add another session - should evict session_1 now (oldest of remaining)
        time.sleep(0.00001)
        container.get_storage("session_5")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Verify session_3 is still present (most recently accessed)
        self.assertIn("session_3", container.index_map)
        self.assertIn("session_4", container.index_map)
        self.assertIn("session_5", container.index_map)
        # session_1 should now be evicted
        self.assertNotIn("session_1", container.index_map)
