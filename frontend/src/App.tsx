import { Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Discover from './pages/Discover';
import Search from './pages/Search';
import Watchlist from './pages/Watchlist';
import Library from './pages/Library';
import Requests from './pages/Requests';
import Wanted from './pages/Wanted';
import Settings from './pages/Settings';
import Login from './pages/Login';
import AdminTabs from './pages/AdminTabs';

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Layout />}>
        <Route index element={<Discover />} />
        <Route path="library" element={<Library />} />
        <Route path="watchlist" element={<Watchlist />} />
        <Route path="search" element={<Search />} />
        <Route path="requests" element={<Requests />} />
        <Route path="wanted" element={<Wanted />} />
        <Route path="settings" element={<Settings />} />
        <Route path="admin" element={<AdminTabs />} />
        <Route path="manual" element={
          <iframe src="/docs/install-guide.html" className="w-full border-0" style={{ height: 'calc(100vh - 57px)' }} />
        } />
        <Route path="*" element={<div className="text-center py-16 text-muted">Page not found</div>} />
      </Route>
    </Routes>
  );
}
