"""Wiki viewer gateway — browse and edit Nova wiki pages as styled web pages.

Standalone HTTP server that reads directly from NovaMemory DB (~/.nova/nova.db).
Uses stdlib http.server + markdown2 for rendering. No Flask dependency.

Routes:
  GET /                    → Homepage: all pages by category + sidebar
  GET /page/{slug}         → Page view: rendered markdown + metadata
  GET /page/{slug}/edit    → Edit form: textarea with raw markdown
  POST /page/{slug}/edit   → Save edit: replace content via wiki_add()
  GET /search?q={query}    → Search results: FTS5 query
  GET /api/pages           → JSON API: wiki_list()
  GET /api/page/{slug}     → JSON API: wiki_read()
"""

import json
import os
import re
import sys
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nova.gateway import ensure_single_instance
from nova.memory.engine import NovaMemory
import markdown2


# ── CSS ──

CSS = """
:root { --sidebar-w: 260px; --bg-sidebar: #1a1a2e; --bg-content: #ffffff;
  --text-sidebar: #e0e0e0; --text-content: #333; --accent: #4a9eff;
  --badge-arch: #4a9eff; --badge-decision: #ff9f43; --badge-pattern: #2ed573;
  --badge-reference: #a55eea; --badge-debugging: #ff4757; --badge-env: #1e90ff;
  --badge-session-log: #747d8c; --badge-convention: #5352ed; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: system-ui, -apple-system, sans-serif; color: var(--text-content);
  display: flex; min-height: 100vh; }
.sidebar { width: var(--sidebar-w); background: var(--bg-sidebar); color: var(--text-sidebar);
  padding: 20px 15px; position: fixed; top: 0; bottom: 0; overflow-y: auto; }
.content { margin-left: var(--sidebar-w); padding: 30px 40px; max-width: 900px; flex: 1; }
.sidebar h1 { font-size: 18px; margin-bottom: 15px; color: var(--accent); letter-spacing: 0.5px; }
.search-box { margin-bottom: 20px; }
.search-box input { width: 100%; padding: 8px 12px; border: 1px solid #444;
  border-radius: 4px; background: #2a2a3e; color: #e0e0e0; font-size: 13px; }
.search-box input:focus { outline: none; border-color: var(--accent); }
.cat-tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 15px; }
.cat-tab { padding: 3px 8px; border-radius: 3px; font-size: 11px; cursor: pointer;
  background: #2a2a3e; color: #999; border: 1px solid transparent; text-decoration: none; }
.cat-tab:hover, .cat-tab.active { color: #fff; border-color: var(--accent); }
.page-list { list-style: none; }
.page-list li { padding: 5px 0; }
.page-list a { color: #ccc; text-decoration: none; font-size: 13px; }
.page-list a:hover { color: var(--accent); }
.page-list a.current { color: var(--accent); font-weight: bold; }
.cat-section { margin-bottom: 12px; }
.cat-section h3 { font-size: 12px; color: #888; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.meta-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 20px;
  padding: 10px 15px; background: #f5f7fa; border-radius: 6px; font-size: 13px; }
.badge { padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; color: #fff; }
.badge.architecture { background: var(--badge-arch); }
.badge.decision { background: var(--badge-decision); }
.badge.pattern { background: var(--badge-pattern); }
.badge.reference { background: var(--badge-reference); }
.badge.debugging { background: var(--badge-debugging); }
.badge.environment { background: var(--badge-env); }
.badge.session-log { background: var(--badge-session-log); }
.badge.convention { background: var(--badge-convention); }
.tags { color: #666; }
.tags span { background: #e8ecf0; padding: 2px 6px; border-radius: 2px; margin-right: 4px; font-size: 11px; }
.confidence { font-size: 11px; }
.confidence.high { color: #2ed573; }
.confidence.medium { color: #ff9f43; }
.confidence.low { color: #ff4757; }
.dates { color: #999; font-size: 11px; }
.page-title { font-size: 28px; font-weight: 700; margin-bottom: 10px; color: #222; }
.page-body { line-height: 1.7; font-size: 15px; }
.page-body h1 { font-size: 24px; margin: 25px 0 10px; border-bottom: 2px solid #eee; padding-bottom: 5px; }
.page-body h2 { font-size: 20px; margin: 20px 0 8px; }
.page-body h3 { font-size: 17px; margin: 15px 0 6px; }
.page-body p { margin: 10px 0; }
.page-body code { background: #f0f3f6; padding: 2px 5px; border-radius: 3px; font-size: 13px; }
.page-body pre { background: #2d2d3d; color: #e0e0e0; padding: 15px; border-radius: 6px;
  overflow-x: auto; margin: 15px 0; font-size: 13px; line-height: 1.5; }
.page-body pre code { background: none; padding: 0; color: inherit; }
.page-body table { border-collapse: collapse; margin: 15px 0; width: 100%; }
.page-body th, .page-body td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
.page-body th { background: #f5f7fa; }
.page-body a { color: var(--accent); }
.page-body a.broken-ref { color: #ff4757; text-decoration: line-through; }
.page-body ul, .page-body ol { margin: 10px 0; padding-left: 25px; }
.page-body blockquote { border-left: 3px solid var(--accent); padding: 5px 15px; margin: 15px 0; color: #666; }
.page-body hr { border: none; border-top: 1px solid #eee; margin: 20px 0; }
.edit-btn { display: inline-block; padding: 6px 14px; background: var(--accent); color: #fff;
  border-radius: 4px; text-decoration: none; font-size: 13px; margin-left: 10px; }
.edit-btn:hover { background: #3a8eef; }
.edit-form textarea { width: 100%; min-height: 400px; padding: 15px; font-family: monospace;
  font-size: 14px; border: 1px solid #ddd; border-radius: 6px; line-height: 1.5; }
.edit-form textarea:focus { outline: none; border-color: var(--accent); }
.edit-actions { margin-top: 10px; display: flex; gap: 10px; }
.btn { padding: 8px 20px; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }
.btn-save { background: var(--accent); color: #fff; }
.btn-cancel { background: #e0e0e0; color: #333; text-decoration: none; }
.search-result { padding: 12px; border-bottom: 1px solid #eee; }
.search-result h3 { font-size: 16px; margin-bottom: 4px; }
.search-result h3 a { color: var(--accent); text-decoration: none; }
.search-result .snippet { color: #666; font-size: 13px; }
.empty-state { text-align: center; padding: 60px; color: #888; }
.empty-state h2 { font-size: 22px; margin-bottom: 10px; }
"""

CATEGORY_COLORS = {
    'architecture': 'architecture',
    'decision': 'decision',
    'pattern': 'pattern',
    'reference': 'reference',
    'debugging': 'debugging',
    'environment': 'environment',
    'session-log': 'session-log',
    'convention': 'convention',
}


# ── HTML Helpers ──

def _badge_class(category):
    return CATEGORY_COLORS.get(category, 'reference')


def md_to_html(content, existing_slugs=None):
    html = markdown2.markdown(
        content,
        extras=["fenced-code-blocks", "tables", "strike", "header-ids"],
        safe_mode=True,
    )
    if existing_slugs:
        def replace_ref(match):
            slug = match.group(1)
            if slug in existing_slugs:
                return f'<a href="/page/{slug}">{slug}</a>'
            return f'<a href="/page/{slug}" class="broken-ref">{slug}</a>'
        html = re.sub(r'\[\[(\w[\w-]*)\]\]', replace_ref, html)
    else:
        html = re.sub(r'\[\[(\w[\w-]*)\]\]', r'<a href="/page/\1">\1</a>', html)
    return html


def render_html(title, body_html, sidebar_html):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title} — Nova Wiki</title>
<style>{CSS}</style></head>
<body>
<div class="sidebar">{sidebar_html}</div>
<div class="content">{body_html}</div>
</body></html>"""


def render_sidebar(pages, current_slug=None, active_cat=None, search_query=None):
    categories = sorted(set(p['category'] for p in pages))
    all_tab = '<a href="/" class="cat-tab active">All</a>' if not active_cat else '<a href="/" class="cat-tab">All</a>'
    cat_tabs = all_tab + ''.join(
        f'<a href="/?cat={c}" class="cat-tab{' active' if active_cat == c else ''}">{c}</a>'
        for c in categories
    )

    filtered = pages if not active_cat else [p for p in pages if p['category'] == active_cat]
    by_cat = {}
    for p in filtered:
        by_cat.setdefault(p['category'], []).append(p)

    sections = ''
    for cat, cat_pages in sorted(by_cat.items()):
        links = ''.join(
            f'<li><a href="/page/{p['slug']}" class="{'current' if p['slug'] == current_slug else ''}">'
            f'{p['title']}</a></li>'
            for p in cat_pages
        )
        sections += f'<div class="cat-section"><h3>{cat}</h3><ul class="page-list">{links}</ul></div>'

    search_val = search_query or ''
    return (
        f'<h1>Nova Wiki</h1>'
        f'<form class="search-box" action="/search" method="get">'
        f'<input type="text" name="q" value="{search_val}" placeholder="Search wiki..."></form>'
        f'<div class="cat-tabs">{cat_tabs}</div>'
        f'{sections}'
    )


def render_homepage(pages, active_cat=None):
    if not pages:
        sidebar = render_sidebar(pages, active_cat=active_cat)
        body = '<div class="empty-state"><h2>No wiki pages yet</h2><p>Create pages using Nova agent tools.</p></div>'
        return render_html('Nova Wiki', body, sidebar)

    recent = pages[:5]
    recent_html = ''.join(
        f'<div class="search-result"><h3><a href="/page/{p['slug']}">{p['title']}</a></h3>'
        f'<span class="badge {_badge_class(p['category'])}">{p['category']}</span> '
        f'<span class="confidence {p['confidence']}">{p['confidence']}</span></div>'
        for p in recent
    )
    body = f'<div class="page-title">Wiki Home</div><div style="margin-bottom:15px;color:#888;font-size:14px">'
    body += f'{len(pages)} pages across {len(set(p['category'] for p in pages))} categories</div>'
    body += f'<h2>Recent Pages</h2>{recent_html}'
    sidebar = render_sidebar(pages, active_cat=active_cat)
    return render_html('Nova Wiki', body, sidebar)


def render_page_view(page_data, html_content, all_pages, all_slugs):
    slug = page_data['slug']
    tags = page_data.get('tags', '')
    tag_spans = ''.join(f'<span>{t.strip()}</span>' for t in tags.split(',') if t.strip()) if tags else ''
    conf = page_data.get('confidence', 'medium')
    sidebar = render_sidebar(all_pages, current_slug=slug)

    meta = (
        f'<span class="badge {_badge_class(page_data['category'])}">{page_data['category']}</span> '
        f'<span class="tags">{tag_spans}</span> '
        f'<span class="confidence {conf}">{conf}</span> '
        f'<span class="dates">updated {page_data.get('updated_at', '')[:10]}</span>'
        f'<a href="/page/{slug}/edit" class="edit-btn">Edit</a>'
    )

    body = (
        f'<div class="page-title">{page_data['title']}</div>'
        f'<div class="meta-bar">{meta}</div>'
        f'<div class="page-body">{html_content}</div>'
    )
    return render_html(page_data['title'], body, sidebar)


def render_edit_form(page_data, all_pages):
    slug = page_data['slug']
    content = page_data.get('content', '')
    sidebar = render_sidebar(all_pages, current_slug=slug)
    # Escape content for textarea (HTML entities)
    escaped = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    body = (
        f'<div class="page-title">Edit: {page_data['title']}</div>'
        f'<form class="edit-form" action="/page/{slug}/edit" method="post">'
        f'<textarea name="content">{escaped}</textarea>'
        f'<div class="edit-actions">'
        f'<button type="submit" class="btn btn-save">Save</button>'
        f'<a href="/page/{slug}" class="btn btn-cancel">Cancel</a>'
        f'</div></form>'
    )
    return render_html(f'Edit: {page_data['title']}', body, sidebar)


def render_search_results(results, query, all_pages, category=None):
    sidebar = render_sidebar(all_pages, search_query=query)
    if not results:
        body = f'<div class="empty-state"><h2>No results for "{query}"</h2><p>Try different keywords.</p></div>'
        return render_html(f'Search: {query}', body, sidebar)

    result_items = ''.join(
        f'<div class="search-result"><h3><a href="/page/{r['slug']}">{r['title']}</a></h3>'
        f'<span class="badge {_badge_class(r['category'])}">{r['category']}</span> '
        f'<div class="snippet">{r['content'][:150].replace(chr(10), ' ')}...</div></div>'
        for r in results
    )
    cat_info = f' in {category}' if category else ''
    body = f'<div class="page-title">Search: "{query}"{cat_info}</div>'
    body += f'<div style="margin-bottom:15px;color:#888;font-size:14px">{len(results)} results</div>'
    body += result_items
    return render_html(f'Search: {query}', body, sidebar)


# ── HTTP Handler ──

class WikiHandler(BaseHTTPRequestHandler):
    memory = None

    def _send_html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def _send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _send_redirect(self, url):
        self.send_response(303)
        self.send_header('Location', url)
        self.end_headers()

    def _send_404(self, msg='Page not found'):
        self.send_response(404)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        html = f'<html><body><h1>404</h1><p>{msg}</p><p><a href="/">Back to wiki home</a></p></body></html>'
        self.wfile.write(html.encode('utf-8'))

    def _get_pages(self):
        return self.memory.wiki_list()

    def _get_slugs(self):
        return {p['slug'] for p in self._get_pages()}

    def do_GET(self):
        path = urllib.parse.unquote(self.path)
        parsed = urllib.parse.urlparse(path)
        params = urllib.parse.parse_qs(parsed.query)

        # Homepage
        if parsed.path == '/' or parsed.path == '':
            pages = self._get_pages()
            active_cat = params.get('cat', [None])[0]
            html = render_homepage(pages, active_cat=active_cat)
            self._send_html(html)
            return

        # Search
        if parsed.path == '/search':
            query = params.get('q', [''])[0]
            category = params.get('cat', [None])[0]
            if not query:
                self._send_redirect('/')
                return
            results = self.memory.wiki_query(query, category=category)
            pages = self._get_pages()
            html = render_search_results(results, query, pages, category=category)
            self._send_html(html)
            return

        # Page view: /page/{slug}
        match = re.match(r'^/page/([\w-]+)$', parsed.path)
        if match:
            slug = match.group(1)
            page_data = self.memory.wiki_read(slug)
            if not page_data:
                self._send_404(f'Wiki page "{slug}" not found')
                return
            all_pages = self._get_pages()
            all_slugs = {p['slug'] for p in all_pages}
            html_content = md_to_html(page_data['content'], existing_slugs=all_slugs)
            html = render_page_view(page_data, html_content, all_pages, all_slugs)
            self._send_html(html)
            return

        # Edit form: /page/{slug}/edit
        match = re.match(r'^/page/([\w-]+)/edit$', parsed.path)
        if match:
            slug = match.group(1)
            page_data = self.memory.wiki_read(slug)
            if not page_data:
                self._send_404(f'Wiki page "{slug}" not found')
                return
            all_pages = self._get_pages()
            html = render_edit_form(page_data, all_pages)
            self._send_html(html)
            return

        # JSON API: /api/pages
        if parsed.path == '/api/pages':
            pages = self._get_pages()
            self._send_json(pages)
            return

        # JSON API: /api/page/{slug}
        match = re.match(r'^/api/page/([\w-]+)$', parsed.path)
        if match:
            slug = match.group(1)
            page_data = self.memory.wiki_read(slug)
            if not page_data:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'not found'}).encode())
                return
            self._send_json(page_data)
            return

        self._send_404()

    def do_POST(self):
        path = urllib.parse.unquote(self.path)

        # Save edit: /page/{slug}/edit
        match = re.match(r'^/page/([\w-]+)/edit$', path)
        if match:
            slug = match.group(1)
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            params = urllib.parse.parse_qs(body)
            new_content = params.get('content', [''])[0]
            # HTML form sends \n as literal strings — convert to real newlines
            new_content = new_content.replace('\\n', '\n')

            if not new_content:
                self._send_404('Empty content')
                return

            page_data = self.memory.wiki_read(slug)
            if not page_data:
                self._send_404(f'Wiki page "{slug}" not found')
                return

            # Delete + re-add for full content replacement (wiki_add merges on conflict, wiki_ingest appends)
            # wiki_delete + wiki_add gives clean replacement using public API
            self.memory.wiki_delete(slug)
            self.memory.wiki_add(
                slug, page_data['title'], new_content,
                category=page_data['category'],
                tags=page_data['tags'],
                confidence=page_data['confidence'],
            )
            self._send_redirect(f'/page/{slug}')
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


# ── Main Entry ──

def main():
    ensure_single_instance(19531, 'Wiki')
    db_path = os.path.join(os.path.expanduser('~'), '.nova', 'nova.db')
    memory = NovaMemory(db_path)
    WikiHandler.memory = memory

    port = int(os.environ.get('NOVA_WIKI_PORT', '8081'))
    server = HTTPServer(('0.0.0.0', port), WikiHandler)
    print(f'Wiki viewer running on port {port}')
    print(f'Open http://localhost:{port} in your browser')
    server.serve_forever()


if __name__ == '__main__':
    main()