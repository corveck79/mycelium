// spore-nfs exposes Mycelium's virtual library as a read-only NFSv3 export.
//
// It carries no media data itself: every Stat() asks Mycelium's existing
// /spore-stream/<token> endpoint (via HEAD) for the real file size, and
// every Read() re-issues that same request with a Range header. Mycelium
// already knows how to serve moov-first cached headers and Range-proxy the
// rest from TorBox (mp4_faststart.py, catbox.materialize()) -- this server
// is only a protocol adapter from NFS reads to those existing HTTP calls.
//
// Because the file Plex reads here has real bytes and a real size, Direct
// Play is the correct outcome instead of the black-screen problem the fake
// stub .mkv approach hits on some clients.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/go-git/go-billy/v5"
	nfs "github.com/willscott/go-nfs"
	nfshelper "github.com/willscott/go-nfs/helpers"
)

var (
	myceliumBase = envOr("MYCELIUM_BASE", "http://mycelium:8088")
	listenAddr   = envOr("LISTEN_ADDR", ":2049")
	treeTTL      = 10 * time.Second
	httpClient   = &http.Client{Timeout: 30 * time.Second}
)

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ---- virtual tree -----------------------------------------------------

type treeEntry struct {
	Token string `json:"token"`
	Path  string `json:"path"` // e.g. "movies/Civil War (2024)/Civil War (2024).mkv"
}

type tree struct {
	mu        sync.RWMutex
	byPath    map[string]string // path -> token
	dirs      map[string]bool   // every ancestor directory of every file
	fetchedAt time.Time
}

func newTree() *tree { return &tree{byPath: map[string]string{}, dirs: map[string]bool{"": true}} }

func (t *tree) refreshIfStale() {
	t.mu.RLock()
	stale := time.Since(t.fetchedAt) > treeTTL
	t.mu.RUnlock()
	if !stale {
		return
	}
	t.refresh()
}

func (t *tree) refresh() {
	resp, err := httpClient.Get(myceliumBase + "/spore-nfs/tree")
	if err != nil {
		log.Printf("tree refresh: %v", err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.Printf("tree refresh: unexpected status %d", resp.StatusCode)
		return
	}
	var out struct {
		Entries []treeEntry `json:"entries"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		log.Printf("tree refresh decode: %v", err)
		return
	}

	byPath := map[string]string{}
	dirs := map[string]bool{"": true}
	for _, e := range out.Entries {
		clean := strings.Trim(path.Clean("/"+e.Path), "/")
		byPath[clean] = e.Token
		for dir := path.Dir(clean); dir != "." && dir != "/"; dir = path.Dir(dir) {
			dirs[dir] = true
			if dir == "." {
				break
			}
		}
	}

	t.mu.Lock()
	t.byPath = byPath
	t.dirs = dirs
	t.fetchedAt = time.Now()
	t.mu.Unlock()
	log.Printf("tree refreshed: %d files, %d dirs", len(byPath), len(dirs))
}

func (t *tree) tokenFor(p string) (string, bool) {
	t.refreshIfStale()
	t.mu.RLock()
	defer t.mu.RUnlock()
	tok, ok := t.byPath[p]
	return tok, ok
}

func (t *tree) isDir(p string) bool {
	t.refreshIfStale()
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.dirs[p]
}

// children returns the immediate child names (files and subdirs) of dir.
func (t *tree) children(dir string) []string {
	t.refreshIfStale()
	t.mu.RLock()
	defer t.mu.RUnlock()
	seen := map[string]bool{}
	var out []string
	add := func(name string) {
		if name != "" && !seen[name] {
			seen[name] = true
			out = append(out, name)
		}
	}
	for p := range t.byPath {
		if path.Dir(p) == dir || (dir == "" && !strings.Contains(p, "/")) {
			add(path.Base(p))
		}
	}
	for d := range t.dirs {
		if d != "" && (path.Dir(d) == dir || (dir == "" && !strings.Contains(d, "/"))) {
			add(path.Base(d))
		}
	}
	return out
}

// ---- HTTP-backed file size / content -----------------------------------

func realSize(token string) (int64, error) {
	req, err := http.NewRequest(http.MethodHead, myceliumBase+"/spore-stream/"+token, nil)
	if err != nil {
		return 0, err
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 && resp.StatusCode != 302 {
		return 0, fmt.Errorf("HEAD %s: status %d", token, resp.StatusCode)
	}
	cl := resp.Header.Get("Content-Length")
	if cl == "" {
		return 0, fmt.Errorf("HEAD %s: no Content-Length", token)
	}
	return strconv.ParseInt(cl, 10, 64)
}

// cheapSize asks Mycelium's TorBox checkcached-backed lookup for a file's
// size WITHOUT materializing it (no torrent add, no CDN URL fetch). Used
// for library scans (Attr/Stat/ReadDir), where realSize()'s materializing
// HEAD would otherwise add every single scanned item to TorBox just to
// learn its size.
func cheapSize(token string) (int64, error) {
	resp, err := httpClient.Get(myceliumBase + "/spore-nfs/size/" + token)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("size lookup %s: status %d", token, resp.StatusCode)
	}
	var out struct {
		Size int64 `json:"size"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return 0, err
	}
	return out.Size, nil
}

// MKV items are served by spore-stream as a 302 to the real TorBox CDN URL
// once warm (offsets map 1:1, no moov rewriting needed for MKV). Chasing
// that redirect on every single NFS read adds a full extra network hop per
// chunk, which at 1MB-ish NFS read sizes adds up to real stutter on a
// 20+Mbps stream. Cache the resolved CDN URL per token and read directly
// from it afterwards -- only ever populated from an *observed* redirect, so
// it's never used for content spore-stream serves itself (e.g. the MP4
// virtual-moov layout, where byte offsets do NOT map to the raw CDN file).
var (
	cdnURLMu    sync.RWMutex
	cdnURLCache = map[string]struct {
		url     string
		expires time.Time
	}{}
)

var noRedirectClient = &http.Client{
	Timeout: 30 * time.Second,
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

func readRange(token string, offset, length int64) ([]byte, error) {
	target := myceliumBase + "/spore-stream/" + token
	cdnURLMu.RLock()
	cached, ok := cdnURLCache[token]
	cdnURLMu.RUnlock()
	if ok && time.Now().Before(cached.expires) {
		target = cached.url
	}

	req, err := http.NewRequest(http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
	resp, err := noRedirectClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusFound || resp.StatusCode == http.StatusMovedPermanently {
		loc := resp.Header.Get("Location")
		if loc == "" {
			return nil, fmt.Errorf("range GET %s: redirect with no Location", token)
		}
		cdnURLMu.Lock()
		cdnURLCache[token] = struct {
			url     string
			expires time.Time
		}{url: loc, expires: time.Now().Add(50 * time.Minute)}
		cdnURLMu.Unlock()

		req2, err := http.NewRequest(http.MethodGet, loc, nil)
		if err != nil {
			return nil, err
		}
		req2.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
		resp2, err := httpClient.Do(req2)
		if err != nil {
			return nil, err
		}
		defer resp2.Body.Close()
		if resp2.StatusCode != 206 && resp2.StatusCode != 200 {
			return nil, fmt.Errorf("range GET (redirected) %s: status %d", token, resp2.StatusCode)
		}
		return io.ReadAll(io.LimitReader(resp2.Body, length))
	}

	if resp.StatusCode != 206 && resp.StatusCode != 200 {
		return nil, fmt.Errorf("range GET %s: status %d", token, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, length))
}

// ---- billy.Filesystem implementation -----------------------------------

type sporeFS struct {
	tree *tree
}

func (fs *sporeFS) clean(p string) string {
	return strings.Trim(path.Clean("/"+filepathToSlash(p)), "/")
}

func filepathToSlash(p string) string { return strings.ReplaceAll(p, "\\", "/") }

func (fs *sporeFS) Root() string { return "/" }

func (fs *sporeFS) Create(filename string) (billy.File, error)      { return nil, billy.ErrReadOnly }
func (fs *sporeFS) OpenFile(filename string, flag int, perm os.FileMode) (billy.File, error) {
	return fs.Open(filename)
}
func (fs *sporeFS) Rename(oldpath, newpath string) error { return billy.ErrReadOnly }
func (fs *sporeFS) Remove(filename string) error         { return billy.ErrReadOnly }
func (fs *sporeFS) Join(elem ...string) string           { return path.Join(elem...) }
func (fs *sporeFS) TempFile(dir, prefix string) (billy.File, error) { return nil, billy.ErrReadOnly }
func (fs *sporeFS) MkdirAll(filename string, perm os.FileMode) error { return nil }
func (fs *sporeFS) Symlink(target, link string) error    { return billy.ErrReadOnly }
func (fs *sporeFS) Readlink(link string) (string, error) { return "", errors.New("not a symlink") }
func (fs *sporeFS) Chroot(path string) (billy.Filesystem, error) { return fs, nil }

func (fs *sporeFS) Open(filename string) (billy.File, error) {
	p := fs.clean(filename)
	tok, ok := fs.tree.tokenFor(p)
	if !ok {
		return nil, os.ErrNotExist
	}
	size, err := realSize(tok)
	if err != nil {
		return nil, err
	}
	return &sporeFile{name: p, token: tok, size: size}, nil
}

func (fs *sporeFS) Stat(filename string) (os.FileInfo, error) {
	p := fs.clean(filename)
	if p == "" || fs.tree.isDir(p) {
		return dirInfo{name: path.Base(p)}, nil
	}
	tok, ok := fs.tree.tokenFor(p)
	if !ok {
		return nil, os.ErrNotExist
	}
	size, err := cheapSize(tok)
	if err != nil {
		return nil, err
	}
	return fileInfo{name: path.Base(p), size: size}, nil
}
func (fs *sporeFS) Lstat(filename string) (os.FileInfo, error) { return fs.Stat(filename) }

func (fs *sporeFS) ReadDir(dirname string) ([]os.FileInfo, error) {
	p := fs.clean(dirname)
	var out []os.FileInfo
	for _, name := range fs.tree.children(p) {
		child := path.Join(p, name)
		if fs.tree.isDir(child) {
			out = append(out, dirInfo{name: name})
			continue
		}
		tok, ok := fs.tree.tokenFor(child)
		if !ok {
			continue
		}
		size, err := cheapSize(tok)
		if err != nil {
			// Item not checkable right now (TorBox/CDN hiccup): still list
			// it so the library entry exists, just report 0 for now.
			size = 0
		}
		out = append(out, fileInfo{name: name, size: size})
	}
	return out, nil
}

// ---- os.FileInfo implementations ---------------------------------------

type fileInfo struct {
	name string
	size int64
}

func (f fileInfo) Name() string       { return f.name }
func (f fileInfo) Size() int64        { return f.size }
func (f fileInfo) Mode() os.FileMode  { return 0444 }
func (f fileInfo) ModTime() time.Time { return time.Unix(0, 0) }
func (f fileInfo) IsDir() bool        { return false }
func (f fileInfo) Sys() interface{}   { return nil }

type dirInfo struct{ name string }

func (d dirInfo) Name() string       { return d.name }
func (d dirInfo) Size() int64        { return 0 }
func (d dirInfo) Mode() os.FileMode  { return os.ModeDir | 0555 }
func (d dirInfo) ModTime() time.Time { return time.Unix(0, 0) }
func (d dirInfo) IsDir() bool        { return true }
func (d dirInfo) Sys() interface{}   { return nil }

// ---- billy.File: reads proxy to spore-stream via Range ------------------

type sporeFile struct {
	name  string
	token string
	size  int64
	pos   int64
	mu    sync.Mutex
}

func (f *sporeFile) Name() string { return f.name }

func (f *sporeFile) Read(p []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.pos >= f.size {
		return 0, io.EOF
	}
	want := int64(len(p))
	if f.pos+want > f.size {
		want = f.size - f.pos
	}
	buf, err := readRange(f.token, f.pos, want)
	if err != nil {
		return 0, err
	}
	n := copy(p, buf)
	f.pos += int64(n)
	return n, nil
}

func (f *sporeFile) ReadAt(p []byte, off int64) (int, error) {
	f.mu.Lock()
	f.pos = off
	f.mu.Unlock()
	return f.Read(p)
}

func (f *sporeFile) Seek(offset int64, whence int) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch whence {
	case io.SeekStart:
		f.pos = offset
	case io.SeekCurrent:
		f.pos += offset
	case io.SeekEnd:
		f.pos = f.size + offset
	}
	return f.pos, nil
}

func (f *sporeFile) Write(p []byte) (int, error)   { return 0, billy.ErrReadOnly }
func (f *sporeFile) Close() error                  { return nil }
func (f *sporeFile) Lock() error                   { return nil }
func (f *sporeFile) Unlock() error                 { return nil }
func (f *sporeFile) Truncate(size int64) error     { return billy.ErrReadOnly }

// ---- main ---------------------------------------------------------------

func main() {
	t := newTree()
	t.refresh()

	// Retry independently of incoming NFS requests: on a fresh start this
	// container can win the race against mycelium's own startup, and
	// refreshIfStale() alone won't retry again until something actually
	// asks the filesystem for a file, which never happens on a client
	// that gave up mounting after an empty first listing.
	go func() {
		ticker := time.NewTicker(treeTTL)
		defer ticker.Stop()
		for range ticker.C {
			t.refresh()
		}
	}()

	fs := &sporeFS{tree: t}
	handler := nfshelper.NewNullAuthHandler(fs)
	cacheHelper := nfshelper.NewCachingHandler(handler, 4096)

	listener, err := net.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("spore-nfs listening on %s, backing store = %s", listenAddr, myceliumBase)
	if err := nfs.Serve(listener, cacheHelper); err != nil {
		log.Fatal(err)
	}
}
