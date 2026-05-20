import { Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Discover from './pages/Discover';
import Search from './pages/Search';
import Watchlist from './pages/Watchlist';
import Library from './pages/Library';
import Requests from './pages/Requests';
import Wanted from './pages/Wanted';
import Admin from './pages/Admin';
import Settings from './pages/Settings';
import Login from './pages/Login';

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
        <Route path="admin" element={<Admin />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<div className="text-center py-16 text-muted">Page not found</div>} />
      </Route>
    </Routes>
  );
}
