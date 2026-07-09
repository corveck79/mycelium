// spore-9p exposes Mycelium's virtual library as a read-only 9P2000 export.
//
// Same principle as spore-nfs (see git history), but 9P is a genuinely
// stateful protocol: a client Twalk/Topen's a file ONCE, gets a fid, and
// reuses that fid for every subsequent Tread until Tclunk. NFSv3 has no such
// concept -- the go-nfs library called Open()+Stat() on every single read,
// which is what forced most of spore-nfs's caching machinery to exist in
// the first place. Here Open()/Close() are called once per real session, as
// they should be.
//
// The actual "fetch bytes lazily from TorBox via Mycelium" logic is
// protocol-agnostic and carried over essentially unchanged from spore-nfs:
// Mycelium's /spore-nfs/tree (virtual directory listing), /spore-nfs/size/
// (cheap, non-materializing size lookup for scans), and /spore-stream/
// (materializing byte source, used for real reads) are the same endpoints.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/knusbaum/go9p"
	"github.com/knusbaum/go9p/fs"
	"github.com/knusbaum/go9p/proto"
)

var (
	myceliumBase = envOr("MYCELIUM_BASE", "http://127.0.0.1:8088")
	listenAddr   = envOr("LISTEN_ADDR", "0.0.0.0:5640")
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
	Path  string `json:"path"`
}

type tree struct {
	mu        sync.RWMutex
	byPath    map[string]string // clean relative path -> token
	dirs      map[string]bool   // every ancestor directory ("" = root)
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

// ---- HTTP-backed size / content (unchanged from spore-nfs) --------------

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

var (
	realSizeCacheMu sync.RWMutex
	realSizeCache   = map[string]struct {
		size    int64
		expires time.Time
	}{}
)

func peekRealSize(token string) (int64, bool) {
	realSizeCacheMu.RLock()
	defer realSizeCacheMu.RUnlock()
	e, ok := realSizeCache[token]
	if !ok || time.Now().After(e.expires) {
		return 0, false
	}
	return e.size, true
}

func cachedRealSize(token string) (int64, error) {
	if size, ok := peekRealSize(token); ok {
		return size, nil
	}
	size, err := realSize(token)
	if err != nil {
		return 0, err
	}
	realSizeCacheMu.Lock()
	realSizeCache[token] = struct {
		size    int64
		expires time.Time
	}{size: size, expires: time.Now().Add(30 * time.Minute)}
	realSizeCacheMu.Unlock()
	return size, nil
}

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

func fetchRange(client *http.Client, url string, offset, length int64) ([]byte, error) {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 206 && resp.StatusCode != 200 {
		return nil, fmt.Errorf("range GET %s: status %d", url, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, length))
}

func readRange(token string, offset, length int64) ([]byte, error) {
	cdnURLMu.RLock()
	cached, ok := cdnURLCache[token]
	cdnURLMu.RUnlock()
	if ok && time.Now().Before(cached.expires) {
		data, err := fetchRange(httpClient, cached.url, offset, length)
		if err == nil {
			return data, nil
		}
		log.Printf("cached CDN url for %s failed (%v), re-resolving via spore-stream", token, err)
		cdnURLMu.Lock()
		delete(cdnURLCache, token)
		cdnURLMu.Unlock()
	}

	target := myceliumBase + "/spore-stream/" + token
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
		return fetchRange(httpClient, loc, offset, length)
	}

	if resp.StatusCode != 206 && resp.StatusCode != 200 {
		return nil, fmt.Errorf("range GET %s: status %d", token, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, length))
}

// ---- grid-aligned read-ahead windows with background prefetch -----------

const (
	readAheadSize      = 16 << 20  // 16MB per window
	readAheadWindows   = 3         // slots per token
	scanProbeThreshold = 256 << 10 // below this, treat as a metadata probe, not streaming
	probeMinFetch      = 1 << 20   // floor for probe-sized fetches
)

type readAheadWindow struct {
	data  []byte
	start int64
	used  int64
}

type readAheadSet struct {
	mu      sync.Mutex
	windows [readAheadWindows]readAheadWindow
	clock   int64
	pending map[int64]bool
}

var (
	readAheadMu sync.Mutex
	readAheads  = map[string]*readAheadSet{}
)

func gridStart(offset int64) int64 {
	return (offset / readAheadSize) * readAheadSize
}

func (s *readAheadSet) findWindow(start int64) (*readAheadWindow, bool) {
	for i := range s.windows {
		if s.windows[i].data != nil && s.windows[i].start == start {
			return &s.windows[i], true
		}
	}
	return nil, false
}

func (s *readAheadSet) store(w readAheadWindow) {
	lru := 0
	for i := range s.windows {
		if s.windows[i].used < s.windows[lru].used {
			lru = i
		}
	}
	s.windows[lru] = w
}

func (s *readAheadSet) prefetch(token string, start, fileSize int64) {
	fetchLen := int64(readAheadSize)
	if start+fetchLen > fileSize {
		fetchLen = fileSize - start
	}
	data, err := readRange(token, start, fetchLen)

	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.pending, start)
	if err != nil {
		return
	}
	s.store(readAheadWindow{data: data, start: start, used: s.clock})
}

func bufferedRead(token string, offset, want, fileSize int64) ([]byte, error) {
	readAheadMu.Lock()
	s, ok := readAheads[token]
	if !ok {
		s = &readAheadSet{pending: map[int64]bool{}}
		readAheads[token] = s
	}
	readAheadMu.Unlock()

	start := gridStart(offset)

	s.mu.Lock()
	defer s.mu.Unlock()
	s.clock++

	w, ok := s.findWindow(start)
	if !ok {
		fetchLen := int64(readAheadSize)
		if want < scanProbeThreshold {
			fetchLen = want
			if fetchLen < probeMinFetch {
				fetchLen = probeMinFetch
			}
		}
		if offset+want-start > fetchLen {
			fetchLen = offset + want - start
		}
		if start+fetchLen > fileSize {
			fetchLen = fileSize - start
		}
		data, err := readRange(token, start, fetchLen)
		if err != nil {
			return nil, err
		}
		nw := readAheadWindow{data: data, start: start, used: s.clock}
		s.store(nw)
		w = &nw
	} else {
		w.used = s.clock
	}

	rel := offset - w.start
	end := rel + want
	if end > int64(len(w.data)) {
		end = int64(len(w.data))
	}

	if rel > int64(len(w.data))/2 {
		next := start + readAheadSize
		if next < fileSize {
			if _, have := s.findWindow(next); !have && !s.pending[next] {
				s.pending[next] = true
				go s.prefetch(token, next, fileSize)
			}
		}
	}
	return w.data[rel:end], nil
}

// ---- 9P filesystem: Dir / File implementations ---------------------------

// sporeDir represents a directory node at a given tree-relative path.
// Children() is computed live from the shared tree cache on every call,
// same principle as spore-nfs's ReadDir -- the tree can change (library
// scan finds something new) without us needing to rebuild a static node
// graph.
type sporeDir struct {
	fs.BaseNode
	sfs  *sporeFS
	path string // "" for root
}

func (d *sporeDir) Children() map[string]fs.FSNode {
	out := map[string]fs.FSNode{}
	for _, name := range d.sfs.tree.children(d.path) {
		child := path.Join(d.path, name)
		if d.sfs.tree.isDir(child) {
			nd := &sporeDir{
				BaseNode: fs.NewBaseNode(d.sfs.fs, d, name, "spore", "spore", 0555|proto.DMDIR),
				sfs:      d.sfs,
				path:     child,
			}
			out[name] = nd
			continue
		}
		tok, ok := d.sfs.tree.tokenFor(child)
		if !ok {
			continue
		}
		out[name] = newSporeFile(d.sfs, d, name, tok)
	}
	return out
}

// sporeFile is a leaf node backed by a token. Stat() reports size via the
// cheap (non-materializing) lookup for scans; Open() materializes for real
// (once per fid, not once per read -- the whole point of doing this over
// 9P instead of NFSv3). Read() is stateless from the fid's perspective:
// windows/CDN-url/size caches all live keyed by token, shared across every
// fid that ever opens this file, so a concurrent second reader (e.g. Plex's
// own background analysis alongside actual playback) benefits from the
// same warm cache instead of needing its own.
type sporeFile struct {
	fs.BaseNode
	sfs   *sporeFS
	token string
}

func newSporeFile(sfs *sporeFS, parent fs.Dir, name, token string) *sporeFile {
	size, err := cheapSize(token)
	if err != nil {
		size = 0
	}
	f := &sporeFile{
		sfs:   sfs,
		token: token,
	}
	f.BaseNode = fs.NewBaseNode(sfs.fs, parent, name, "spore", "spore", 0444)
	st := f.FStat
	st.Length = uint64(size)
	f.FStat = st
	return f
}

func (f *sporeFile) Stat() proto.Stat {
	st := f.BaseNode.Stat()
	// Prefer the real (materialized) size if we already have it cached from
	// an actual Open() -- more accurate than the cheap scan-time estimate,
	// and keeps a rescan after playback consistent with what was played.
	if size, ok := peekRealSize(f.token); ok {
		st.Length = uint64(size)
		return st
	}
	if size, err := cheapSize(f.token); err == nil {
		st.Length = uint64(size)
	}
	return st
}

func (f *sporeFile) WriteStat(s *proto.Stat) error { return nil }

func (f *sporeFile) Open(fid uint64, omode proto.Mode) error {
	// Materializes in TorBox if needed. Called once per fid (per real
	// playback/analysis session), not once per read -- unlike spore-nfs,
	// which had to work around NFSv3 calling this on every single read.
	_, err := cachedRealSize(f.token)
	return err
}

func (f *sporeFile) Read(fid uint64, offset uint64, count uint64) ([]byte, error) {
	size, err := cachedRealSize(f.token)
	if err != nil {
		return nil, err
	}
	if int64(offset) >= size {
		return []byte{}, nil
	}
	want := int64(count)
	if int64(offset)+want > size {
		want = size - int64(offset)
	}
	return bufferedRead(f.token, int64(offset), want, size)
}

func (f *sporeFile) Write(fid uint64, offset uint64, data []byte) (uint32, error) {
	return 0, fmt.Errorf("read-only")
}

func (f *sporeFile) Close(fid uint64) error { return nil }

type sporeFS struct {
	fs    *fs.FS
	tree  *tree
}

func main() {
	t := newTree()
	t.refresh()
	go func() {
		ticker := time.NewTicker(treeTTL)
		defer ticker.Stop()
		for range ticker.C {
			t.refresh()
		}
	}()

	nineFS, root := fs.NewFS("spore", "spore", 0555)
	sfs := &sporeFS{fs: nineFS, tree: t}
	nineFS.Root = &sporeDir{
		BaseNode: fs.NewBaseNode(nineFS, nil, "/", "spore", "spore", 0555|proto.DMDIR),
		sfs:      sfs,
		path:     "",
	}
	_ = root // StaticDir returned by NewFS is discarded; we replace Root above.

	log.Printf("spore-9p listening on %s, backing store = %s", listenAddr, myceliumBase)
	if err := go9p.Serve(listenAddr, nineFS.Server()); err != nil {
		log.Fatal(err)
	}
}
