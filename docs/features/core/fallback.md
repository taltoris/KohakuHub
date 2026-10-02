---
title: External Source Fallback
description: Browse HuggingFace or other KohakuHub instances when repositories aren't found locally
icon: i-carbon-connect
---

# External Source Fallback

Browse HuggingFace or other KohakuHub instances when repositories aren't found locally.

---

## Overview

The fallback system allows transparent access to external repositories:

**When a repo isn't found locally:**
1. Check configured fallback sources (by priority)
2. If found externally, return with `_source` tag
3. Cache the repo→source mapping (5min TTL)
4. Downloads redirect to external source

**Supported sources:**
- HuggingFace (huggingface.co)
- Other KohakuHub instances

---

## Setup via Admin Portal

1. Navigate to: **Admin Portal → Fallback Sources**
2. Click **Add Source**
3. Configure:
   ```
   Name: HuggingFace
   URL: https://huggingface.co
   Source Type: huggingface
   Priority: 1
   Token: (optional - for private repos)
   Namespace: (empty = global, or specify user/org)
   Enabled: ✓
   ```
4. Click **Create**

---

## Configuration

### Environment Variables

```yaml
KOHAKU_HUB_FALLBACK_ENABLED: true
KOHAKU_HUB_FALLBACK_CACHE_TTL: 300  # 5 minutes
KOHAKU_HUB_FALLBACK_TIMEOUT: 10     # Request timeout
KOHAKU_HUB_FALLBACK_MAX_CONCURRENT: 5

KOHAKU_HUB_FALLBACK_SOURCES: |
  [
    {
      "url": "https://huggingface.co",
      "token": "",
      "priority": 1,
      "name": "HuggingFace",
      "source_type": "huggingface"
    }
  ]
```

### Multiple Sources

Lower priority = checked first:

```json
[
  {"url": "https://internal-hub.company.com", "priority": 1, "source_type": "kohakuhub"},
  {"url": "https://huggingface.co", "priority": 2, "source_type": "huggingface"}
]
```

### Per-Namespace Sources

Only active for specific user/org:

```json
{
  "namespace": "ml-team",
  "url": "https://team-hub.company.com",
  "priority": 1,
  "source_type": "kohakuhub"
}
```

---

## Supported Operations

### Repository Operations

✅ **File Download** - `GET /{type}s/{ns}/{name}/resolve/{rev}/{path}`
✅ **File Tree** - `GET /api/{type}s/{ns}/{name}/tree/{rev}`
✅ **Repo Info** - `GET /api/{type}s/{ns}/{name}`
✅ **Revision Info** - `GET /api/{type}s/{ns}/{name}/revision/{rev}`

### User/Org Operations

✅ **User Profile** - `GET /api/users/{username}/profile`
- HuggingFace: `/api/users/{name}/overview`
- Detects user vs org via member list

✅ **User Repos** - `GET /api/users/{username}/repos`
- HuggingFace: Aggregates `/api/models`, `/api/datasets`, `/api/spaces`

### List Aggregation

✅ **Repository Lists** - `GET /api/models?author={name}`
- Merges local + external with `_source` tags
- Disabled on homepage (`?fallback=false`)

---

## URL Mapping (HuggingFace)

HuggingFace has asymmetric URLs:

| Operation | KohakuHub | HuggingFace |
|-----------|-----------|-------------|
| Models download | `/models/{ns}/{name}/resolve/...` | `/{ns}/{name}/resolve/...` |
| Datasets download | `/datasets/{ns}/{name}/resolve/...` | `/datasets/{ns}/{name}/resolve/...` |
| API endpoints | `/api/{type}s/...` | `/api/{type}s/...` |

Automatic transformation by fallback client!

---

## User Experience

### External Repository Pages

**Indicators:**
- Badge in header: `[☁️ External: https://huggingface.co]`
- Disabled commits tab (not available)
- Metadata tab works (YAML frontmatter parsed)
- **Import to my hub** button, for a signed-in user: copies the repository into
  their namespace as one commit, so the fallback proxy stops being involved for
  it (see [Import](import.md))

### External User/Org Pages

**Shows:**
- Badge in profile: `[☁️ HuggingFace]`
- "Limited profile" note (bio/website may be missing)
- All repos with source tags
- Avatar from HuggingFace (if available)

### Cache System

**What's cached:** Repo→source mapping (NOT content)
**Cache key:** `fallback:repo:{type}:{namespace}/{name}`
**TTL:** 300 seconds (configurable)
**Hit rate:** >80% expected

---

## Query Parameters

Disable fallback per-request:

```
GET /api/models?fallback=false  # Local only
GET /api/models?fallback=true   # With external (default)
```

Used on:
- Homepage trending (local only)
- Main browse pages (local only)
- User/org pages (with external)

---

## Security

**User credentials are NEVER sent to external sources:**
- ❌ No user token passthrough
- ✅ Only admin-configured tokens
- ✅ Public repos work without tokens

**Private repo access:**
- Admin configures HuggingFace token once
- Used for all external requests
- Can access HF private repos (if token permits)

---

## Performance

**Response times:**
- Cache hit: <100ms
- Cache miss: <2s (external API call)
- Download: 302 redirect (full speed)

**Cache management:**
- Auto-expires after TTL
- Manual clear: Admin Portal → Cache Stats → Clear
- View stats: Size, usage%, hit rate

---

## Admin Interface

**Fallback Sources page:**

**Features:**
- Add/edit/delete sources
- Enable/disable toggle
- Priority ordering
- Cache statistics
- Manual cache clear

**Cache stats:**
- Current size / max size (10,000)
- TTL seconds
- Usage percentage

---

## Troubleshooting

**External repos not showing:**
1. Check sources enabled in admin
2. Verify source URL correct
3. Clear cache and retry
4. Check backend logs for errors

**Downloads failing:**
1. HuggingFace returns 307 redirect (expected)
2. Verify network access to HF
3. Check timeout settings

**Wrong repos shown:**
1. Check `author=` parameter passed correctly
2. Verify HuggingFace API response
3. Check aggregation logs

---

## API Reference

**Admin endpoints:**

```
POST   /admin/api/fallback-sources        # Create
GET    /admin/api/fallback-sources        # List
GET    /admin/api/fallback-sources/{id}   # Get
PUT    /admin/api/fallback-sources/{id}   # Update
DELETE /admin/api/fallback-sources/{id}   # Delete
GET    /admin/api/fallback-sources/cache/stats   # Stats
DELETE /admin/api/fallback-sources/cache/clear  # Clear
```

**Response fields:**

```json
{
  "id": "repo-id",
  "_source": "HuggingFace",
  "_source_url": "https://huggingface.co",
  "_partial": true  // For user profiles with missing fields
}
```

---

See also: [Admin Portal](../api/admin.md), [Configuration Reference](../reference/config.md), [Import](import.md)
