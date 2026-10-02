<script setup>
import MarkdownPage from "@/components/common/MarkdownPage.vue";

const baseUrl = window.location.origin;

const content = `# Get Started with KohakuHub

Welcome to KohakuHub - your self-hosted AI model and dataset hub! This guide covers everything from account creation to advanced features.

---

## 🚀 Quick Start

### 1. Create an Account

**Register your account:**
1. Click **Register** in the navigation bar
2. Enter username, email, and password
3. If invitation-only mode is enabled, you'll need an invitation link
4. Log in with your credentials

**Note:** Check with your administrator if email verification is required.

### 2. Create an Access Token

**For CLI and API access:**
1. Click your username → **Settings**
2. Navigate to **Access Tokens**
3. Click **Create New Token**
4. Name your token (e.g., "Dev Machine", "CI/CD")
5. Copy and save it securely (you can't view it again!)

**Token permissions:**
- Read: Download public/private repos
- Write: Upload, create repos, manage content

---

## 📦 Using CLI Tools

### Environment Setup

\`\`\`bash
# Point all HuggingFace tools to this KohakuHub instance
export HF_ENDPOINT=${baseUrl}
export HF_TOKEN=your_access_token_here

# Make it permanent
echo 'export HF_ENDPOINT=${baseUrl}' >> ~/.bashrc
\`\`\`

### huggingface-cli (Official Tool)

\`\`\`bash
# Install
pip install -U huggingface_hub

# Login
huggingface-cli login

# Download model
huggingface-cli download username/model-name

# Upload file
huggingface-cli upload username/model-name model.safetensors

# Create repo
huggingface-cli repo create username/my-model --type model
\`\`\`

### kohub-cli (Native Tool)

The CLI lives in its own repository,
[KohakuBlueleaf/Kohub-CLI](https://github.com/KohakuBlueleaf/Kohub-CLI): it is
a client of this API, so this server's install does not provide it.

\`\`\`bash
# Install (from its own repository)
git clone https://github.com/KohakuBlueleaf/Kohub-CLI
cd Kohub-CLI
pip install -e .

# Interactive TUI mode
kohub-cli interactive

# Command mode
kohub-cli auth login
kohub-cli repo create username/my-model --type model
kohub-cli repo list --type model --author username
kohub-cli repo delete username/old-model --type model

# Organization management
kohub-cli org create my-team
kohub-cli org member add my-team alice --role admin
kohub-cli org member list my-team

# Settings
kohub-cli settings repo lfs threshold my-org/my-model --threshold 10000000
kohub-cli settings repo lfs suffix my-org/my-model --add .safetensors
\`\`\`

---

## 🐍 Using Python Libraries

### HuggingFace Hub Library

\`\`\`python
import os
os.environ['HF_ENDPOINT'] = '${baseUrl}'
os.environ['HF_TOKEN'] = 'your_token_here'

from huggingface_hub import HfApi, hf_hub_download, snapshot_download

api = HfApi()

# Create repository
api.create_repo("username/my-model", repo_type="model", private=False)

# Upload file
api.upload_file(
    path_or_fileobj="model.safetensors",
    path_in_repo="model.safetensors",
    repo_id="username/my-model"
)

# Download file
model_path = hf_hub_download(
    repo_id="username/my-model",
    filename="model.safetensors"
)

# Download entire repo
repo_path = snapshot_download(repo_id="username/my-model")
\`\`\`

### Transformers & Diffusers

\`\`\`python
import os
os.environ['HF_ENDPOINT'] = '${baseUrl}'
os.environ['HF_TOKEN'] = 'your_token_here'

# Transformers
from transformers import AutoModel, AutoTokenizer
model = AutoModel.from_pretrained("username/my-model")
tokenizer = AutoTokenizer.from_pretrained("username/my-model")

# Diffusers
from diffusers import StableDiffusionPipeline
pipeline = StableDiffusionPipeline.from_pretrained("username/my-sd-model")
\`\`\`

---

## 🌐 Web Interface Features

### Browse & Discover

**Homepage:**
- 🔥 Trending repositories (by recent activity)
- Top models, datasets, and spaces
- Quick access to browse all types

**Browse Pages:**
- **/models** - All public models
- **/datasets** - All datasets
- **/spaces** - All spaces
- Search and filter by name
- Sort by: Recently Updated, Most Downloads, Most Likes

**External Content:**
- Browse HuggingFace repos (if fallback enabled)
- Download from external sources
- View external user profiles
- Repos tagged with source indicator

### Repository Viewer

**Tabs:**
- **Model Card** - README with YAML metadata support
- **Files** - Browse file tree, upload files
- **Commits** - View commit history
- **Metadata** - All YAML frontmatter fields displayed

**YAML Metadata Support:**
- License, languages, framework, pipeline tags
- Base models, training datasets
- Task categories, size info (datasets)
- Evaluation metrics
- All fields displayed in dedicated cards or grid

**Features:**
- Syntax highlighting for code files
- Markdown rendering with Mermaid diagrams
- LaTeX math support
- File preview for text/code
- Download files or entire repo

### Create & Upload

**Create Repository:**
1. Click **New** in navbar
2. Choose type: Model / Dataset / Space
3. Enter name (auto-validates)
4. Select organization (optional)
5. Set visibility: Public / Private
6. Repository created instantly

**Upload Files:**
1. Navigate to repo → **Files** tab
2. Click **Upload Files**
3. Drag & drop or browse
4. Add commit message
5. Files uploaded with automatic LFS handling

**Automatic LFS:**
- Files **>=5MB** use LFS by default
- Or files with: .safetensors, .bin, .pt, .onnx, .zip, .tar, .gz, .parquet, .arrow, etc.
- Configure per-repo in Settings

### Profile & Settings

**User Profile:**
- Avatar upload (1024x1024 JPEG)
- Full name, bio, website
- Social links: Twitter/X, Threads, GitHub, HuggingFace
- View your models, datasets, spaces
- Storage quota (separate public/private)

**Repository Settings:**
- Visibility (public/private)
- Delete repository
- Transfer ownership
- LFS threshold (per-repo override)
- LFS keep versions (garbage collection)
- LFS suffix rules (force LFS for specific extensions)

---

## 👥 Organizations

### Create Organization

\`\`\`bash
# Via CLI
kohub-cli org create my-team --description "Our AI Team"

# Or via web: Click "Organizations" → "New Organization"
\`\`\`

### Manage Members

**Roles:**
- **Member** - View org repos, create repos
- **Admin** - Manage members, settings
- **Super Admin** - Full control

**Add Members:**
1. Navigate to org page
2. Click **Settings** (admin only)
3. Add member by username
4. Set role

**Via CLI:**
\`\`\`bash
kohub-cli org member add my-team alice --role admin
kohub-cli org member remove my-team bob
kohub-cli org member list my-team
\`\`\`

### Organization Features

- Separate storage quotas (public/private)
- Shared repositories
- Team avatars
- Organization profile page
- Member management

---

## 🔄 Git Clone Support

**Pure Python Git server** - no pygit2/libgit2 needed!

\`\`\`bash
# Clone repository
git clone ${baseUrl}/username/repo-name.git

# For private repos
git clone http://username:your-token@${baseUrl.replace("http://", "")}/username/private-repo.git

# Large files use Git LFS
cd repo-name
git lfs install
git lfs pull  # Download files >=1MB
\`\`\`

**How it works:**
- Files <1MB: Direct in Git pack
- Files >=1MB: LFS pointers (lazy download)
- Memory-efficient (handles any repo size)

---

## ⚡ Advanced Features

### Repository Likes

**Like repos you find useful:**
- Click ❤️ on any repository
- View your liked repos: \`/users/{username}/likes\`
- See who liked a repo

### Trending System

**Discover popular repos:**
- Homepage shows trending (7-day activity)
- Algorithm: recent downloads + likes with time decay
- Updated in real-time

### Invitations

**Invite-only mode:**
- Admin creates invitation links
- Single-use or multi-use (set max_usage)
- Can auto-add to organizations
- Expiration dates supported

**Create invitation (admin):**
\`\`\`bash
curl -X POST ${baseUrl}/admin/api/invitations/register \\
  -H "X-Admin-Token: admin_token" \\
  -d '{"max_usage": 10, "expires_days": 30}'
\`\`\`

### SSH Keys

**Add SSH key for Git operations:**
1. Settings → SSH Keys
2. Paste public key
3. Use for Git clone/pull/push

### Branch Operations

**Via Web UI:**
- Create branch
- Delete branch
- Revert commit
- Reset branch to commit
- Cherry-pick commits
- Tag commits

**Via API:**
\`\`\`python
# Create branch
api.create_branch("username/repo", branch="dev", revision="main")

# Create tag
api.create_tag("username/repo", tag="v1.0", revision="abc123")
\`\`\`

### External Fallback

**Browse HuggingFace without importing:**
- Visit \`/openai\` to see OpenAI's HF profile
- Click any HF model to view/download
- Repos tagged with source indicator
- Automatic redirect to external downloads

**How to enable:** Admin Portal → Fallback Sources → Add HuggingFace

---

## 🛠️ Repository Management

### Move Repository

**Change namespace:**
\`\`\`bash
# Via API
curl -X POST ${baseUrl}/api/repos/move \\
  -d '{"from_namespace": "alice", "from_name": "model1",
       "to_namespace": "my-org", "to_name": "model1",
       "repo_type": "model"}'
\`\`\`

### Squash Commits

**Clean up history:**
- Keeps only latest version of each file
- Reduces storage usage
- Cannot be undone!

### Soft Delete

**Files are soft-deleted:**
- Marked as deleted, not physically removed
- Can be restored from history
- Actual deletion via garbage collection

---

## 📊 Quota Management

**Storage limits:**
- **Public repos**: Separate quota
- **Private repos**: Separate quota
- Per-user and per-organization
- View usage: Settings → Storage

**Check quota:**
\`\`\`bash
curl ${baseUrl}/api/quota/username/public
\`\`\`

---

## 🎨 Customization

### Profile

- Upload 1024x1024 avatar (JPEG)
- Add bio (Markdown supported)
- Website URL
- Social media: Twitter/X, Threads, GitHub, HuggingFace

### Site

- Configurable site name
- Custom branding (admin-configured)

---

## 🔧 Troubleshooting

**Repository not found:**
- Check spelling and namespace
- Verify it's public or you have access
- Try external fallback (if enabled)

**Upload fails:**
- Check storage quota
- Verify file formats
- Try smaller batches

**LFS issues:**
- Install git-lfs: \`git lfs install\`
- Pull large files: \`git lfs pull\`
- Check repo LFS settings

**Performance:**
- Use trending sort for discovery
- Enable caching in client
- Batch uploads when possible

---

## 📚 Resources

- **API Docs:** ${baseUrl}/docs
- **Admin Portal:** ${baseUrl}/admin
- **GitHub:** https://github.com/KohakuBlueleaf/KohakuHub
- **Discord:** https://discord.gg/xWYrkyvJ2s
- **Documentation:** ${baseUrl}/docs

---

**Happy model hosting! 🚀**
`;
</script>

<template>
  <MarkdownPage :content="content" />
</template>
