import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { useAuth } from '../context/AuthContext';
import { GoogleLogin } from '@react-oauth/google';
import { Activity, Zap, LogOut, RefreshCw, ShieldCheck, Users, Target, AlertTriangle, Loader2 } from 'lucide-react';

const INDICES = ['NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY'];

const isLocal = typeof window !== 'undefined' && (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1');
const BACKEND_HOST = isLocal ? 'http://localhost:8000' : 'https://optionsathitool.onrender.com';
const API_BASE_URL = `${BACKEND_HOST}/api/v1`;
const WS_BASE_URL = isLocal ? 'ws://localhost:8000' : 'wss://optionsathitool.onrender.com';

export default function Dashboard() {
  const { user, serverWakingUp, loginWithGoogleToken, logout } = useAuth();
  const [activeTab, setActiveTab] = useState('TRADING'); // 'TRADING' or 'ADMIN'
  const [selectedIndex, setSelectedIndex] = useState('NIFTY');
  const [liveSpot, setLiveSpot] = useState(0.0);
  const [pcr, setPcr] = useState(1.0);
  const [regime, setRegime] = useState('NEUTRAL');
  const [activeSignal, setActiveSignal] = useState(null);
  const [signalsLog, setSignalsLog] = useState([]);
  const [loadingDecode, setLoadingDecode] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);

  // Admin Panel States
  const [adminStats, setAdminStats] = useState({ total_users: 0, total_trades: 0, target_hits: 0, sl_hits: 0 });
  const [usersList, setUsersList] = useState([]);
  const [selectedAdminUser, setSelectedAdminUser] = useState(null);
  const [adminUserTrades, setAdminUserTrades] = useState([]);

  const wsRef = useRef(null);

  const fetchSignalsLog = async () => {
    try {
      const res = await axios.get(`${API_BASE_URL}/signals/automated-signals-log`);
      if (res.data && res.data.success) {
        setSignalsLog(res.data.logs || []);
      }
    } catch (e) {
      console.error("Fetch Signals Error:", e);
    }
  };

  const fetchAdminData = async () => {
    try {
      const [statsRes, usersRes] = await Promise.all([
        axios.get(`${API_BASE_URL}/signals/admin/today-stats`),
        axios.get(`${API_BASE_URL}/auth/admin/users-list`)
      ]);
      if (statsRes.data.success) setAdminStats(statsRes.data.stats);
      if (usersRes.data.success) setUsersList(usersRes.data.users);
    } catch (e) {
      console.error("Admin Fetch Error:", e);
    }
  };

  const fetchUserTradesForAdmin = async (userId) => {
    try {
      const res = await axios.get(`${API_BASE_URL}/signals/admin/user-trades/${userId}`);
      if (res.data.success) {
        setAdminUserTrades(res.data.trades);
      }
    } catch (e) {
      console.error("Fetch User Trades Error:", e);
    }
  };

  const connectWebSocket = (indexName) => {
    if (wsRef.current) wsRef.current.close();
    const wsUrl = `${WS_BASE_URL}/api/v1/market/ws/${indexName}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => setWsConnected(true);
    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'TICKER_STREAM') {
          const indexStore = message.data || {};
          if (indexStore.spot && indexStore.spot > 0) {
            setLiveSpot(indexStore.spot);
          }
        } else if (message.type === 'SIGNAL_STATUS_UPDATE') {
          fetchSignalsLog();
          if (user?.role === 'admin') fetchAdminData();
        }
      } catch (e) {}
    };
    ws.onclose = () => setWsConnected(false);
  };

  useEffect(() => {
    if (user) {
      connectWebSocket(selectedIndex);
      fetchSignalsLog();
      if (user.role === 'admin') fetchAdminData();
    }
    return () => { if (wsRef.current) wsRef.current.close(); };
  }, [selectedIndex, user]);

  const handleDecodeSignal = async (isForce = false) => {
    setLoadingDecode(true);
    try {
      const endpoint = isForce ? `${API_BASE_URL}/signals/decode-force` : `${API_BASE_URL}/signals/decode`;
      const res = await axios.post(endpoint, { index_name: selectedIndex });
      if (res.data && res.data.success) {
        setActiveSignal(res.data.data);
        fetchSignalsLog();
      }
    } catch (e) {
      console.error("Decode Signal Error:", e);
    } finally {
      setLoadingDecode(false);
    }
  };

  // 🟢 1. Render Server Wake-Up Screen
  if (serverWakingUp) {
    return (
      <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center p-6 text-center">
        <div className="relative mb-6">
          <div className="w-20 h-20 rounded-full border-4 border-indigo-500/20 border-t-indigo-500 animate-spin"></div>
          <Zap className="w-8 h-8 text-indigo-400 absolute inset-0 m-auto" />
        </div>
        <h2 className="text-2xl font-bold text-slate-100">Waking Up OptionSaathi AI Engine...</h2>
        <p className="text-slate-400 text-sm mt-2 max-w-md">
          Render Cloud Instance start ho raha hai. Isme 20-30 seconds lag sakte hain. Kripya thoda wait karein...
        </p>
      </div>
    );
  }

  // 🟢 2. Mandatory Login Screen Guard
  if (!user) {
    return (
      <div className="min-h-screen bg-slate-950 flex flex-col justify-between items-center p-6">
        <header className="w-full max-w-6xl flex justify-between items-center py-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center">
              <Zap className="text-indigo-400 w-6 h-6" />
            </div>
            <span className="text-2xl font-bold bg-gradient-to-r from-indigo-400 to-cyan-400 bg-clip-text text-transparent">
              OptionSaathi AI
            </span>
          </div>
        </header>

        <div className="max-w-md w-full bg-slate-900/80 border border-slate-800 rounded-3xl p-8 text-center backdrop-blur-xl shadow-2xl">
          <div className="w-16 h-16 bg-indigo-600/20 rounded-2xl border border-indigo-500/30 flex items-center justify-center mx-auto mb-6">
            <Activity className="w-8 h-8 text-indigo-400" />
          </div>
          <h2 className="text-2xl font-bold text-slate-100 mb-2">Access AI Signals Engine</h2>
          <p className="text-slate-400 text-sm mb-6">
            Real-time multi-index options confluence matrix, forced scalping, and automated trade logging access karne ke liye login karein.
          </p>
          <div className="flex justify-center">
            <GoogleLogin
              onSuccess={(res) => loginWithGoogleToken(res.credential)}
              onError={() => console.log('Login Failed')}
            />
          </div>
        </div>

        <p className="text-xs text-slate-500">© 2026 OptionSaathi AI Engine. All rights reserved.</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans">
      {/* Top Header */}
      <header className="border-b border-slate-800 bg-slate-900/50 px-6 py-4 flex justify-between items-center backdrop-blur-md">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center">
              <Zap className="text-indigo-400 w-6 h-6" />
            </div>
            <span className="text-xl font-bold bg-gradient-to-r from-indigo-400 to-cyan-400 bg-clip-text text-transparent">
              OptionSaathi AI
            </span>
            <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold flex items-center gap-1 ${wsConnected ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-amber-500/20 text-amber-400'}`}>
              <span className={`w-2 h-2 rounded-full ${wsConnected ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'}`}></span>
              {wsConnected ? 'LIVE FEED' : 'CONNECTING'}
            </span>
          </div>

          {/* Navigation Tabs (Admin Mode Guard) */}
          {user.role === 'admin' && (
            <div className="flex gap-2 bg-slate-950 p-1 rounded-xl border border-slate-800 ml-4">
              <button
                onClick={() => setActiveTab('TRADING')}
                className={`px-4 py-1.5 rounded-lg text-xs font-bold transition-all ${activeTab === 'TRADING' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-200'}`}
              >
                TRADING ENGINE
              </button>
              <button
                onClick={() => setActiveTab('ADMIN')}
                className={`px-4 py-1.5 rounded-lg text-xs font-bold transition-all flex items-center gap-1 ${activeTab === 'ADMIN' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-200'}`}
              >
                <ShieldCheck className="w-3.5 h-3.5 text-amber-400" /> ADMIN PANEL
              </button>
            </div>
          )}
        </div>

        <div className="flex items-center gap-4 bg-slate-800/60 px-4 py-2 rounded-xl border border-slate-700">
          <img src={user.picture} alt={user.full_name} className="w-8 h-8 rounded-full border border-indigo-400" />
          <div className="text-left">
            <p className="text-sm font-semibold text-slate-200">{user.full_name}</p>
            <p className="text-xs text-slate-400">{user.email}</p>
          </div>
          <button onClick={logout} className="ml-2 text-slate-400 hover:text-red-400 transition-colors">
            <LogOut className="w-5 h-5" />
          </button>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8">
        {activeTab === 'TRADING' ? (
          <>
            {/* Multi-Index Selector Tabs */}
            <div className="flex gap-3 mb-8 bg-slate-900/80 p-1.5 rounded-2xl border border-slate-800 w-fit">
              {INDICES.map((idx) => (
                <button
                  key={idx}
                  onClick={() => setSelectedIndex(idx)}
                  className={`px-6 py-2.5 rounded-xl text-sm font-semibold transition-all ${selectedIndex === idx ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/30' : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'}`}
                >
                  {idx}
                </button>
              ))}
            </div>

            {/* Live Ticker Grid */}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
              <div className="bg-slate-900/60 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-medium text-slate-400 mb-1">{selectedIndex} SPOT PRICE</p>
                <p className="text-3xl font-extrabold text-cyan-400 tracking-tight">
                  {liveSpot > 0 ? `₹${liveSpot.toLocaleString('en-IN')}` : 'Fetching...'}
                </p>
              </div>
              <div className="bg-slate-900/60 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-medium text-slate-400 mb-1">PCR SENTIMENT</p>
                <p className="text-3xl font-bold text-indigo-400">{pcr}</p>
              </div>
              <div className="bg-slate-900/60 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-medium text-slate-400 mb-1">MARKET REGIME</p>
                <p className="text-2xl font-bold text-emerald-400">{regime}</p>
              </div>
              <div className="bg-slate-900/60 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-medium text-slate-400 mb-1">INDIA VIX</p>
                <p className="text-3xl font-bold text-amber-400">13.5</p>
              </div>
            </div>

            {/* AI Signal Controls */}
            <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-8 mb-8 shadow-2xl">
              <div className="flex justify-between items-center mb-6">
                <div>
                  <h2 className="text-2xl font-bold text-slate-100 flex items-center gap-2">
                    <Activity className="text-indigo-400" /> AI Confluence Engine
                  </h2>
                  <p className="text-slate-400 text-sm mt-1">Real-time Options Analytics & Confluence Matrix</p>
                </div>
                <div className="flex gap-3">
                  <button
                    disabled={loadingDecode}
                    onClick={() => handleDecodeSignal(false)}
                    className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white font-semibold px-6 py-3 rounded-xl transition-all shadow-lg shadow-indigo-600/30"
                  >
                    {loadingDecode ? 'Scanning Market...' : 'DECODE SIGNAL'}
                  </button>
                  <button
                    disabled={loadingDecode}
                    onClick={() => handleDecodeSignal(true)}
                    className="bg-amber-600/20 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 font-semibold px-5 py-3 rounded-xl transition-all"
                  >
                    FORCED SCALP
                  </button>
                </div>
              </div>

              {activeSignal && (
                <div className="bg-slate-950/80 border border-slate-800 p-6 rounded-2xl mt-4">
                  <div className="flex justify-between items-start mb-4">
                    <div>
                      <span className={`px-3 py-1 rounded-full text-xs font-bold ${activeSignal.signal === 'BUY CALL' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-red-500/20 text-red-400 border border-red-500/30'}`}>
                        {activeSignal.signal}
                      </span>
                      <h3 className="text-3xl font-bold text-slate-100 mt-2">{activeSignal.strike}</h3>
                    </div>
                    <div className="text-right">
                      <p className="text-xs text-slate-400">ENTRY PREMIUM</p>
                      <p className="text-3xl font-extrabold text-cyan-400">₹{activeSignal.entry_price}</p>
                    </div>
                  </div>

                  <div className="grid grid-cols-3 gap-4 bg-slate-900/60 p-4 rounded-xl border border-slate-800 text-center">
                    <div>
                      <p className="text-xs text-slate-400">STOP LOSS</p>
                      <p className="text-lg font-bold text-red-400">₹{activeSignal.stop_loss}</p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-400">TARGET 1</p>
                      <p className="text-lg font-bold text-emerald-400">₹{activeSignal.shz_upper}</p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-400">TARGET 2</p>
                      <p className="text-lg font-bold text-emerald-300">₹{activeSignal.target2 || '-'}</p>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* Trade Logs Table */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-3xl p-6">
              <div className="flex justify-between items-center mb-4">
                <h3 className="text-lg font-bold text-slate-200">Today's Automated Trade Logs</h3>
                <button onClick={fetchSignalsLog} className="text-slate-400 hover:text-indigo-400 flex items-center gap-1 text-xs">
                  <RefreshCw className="w-3.5 h-3.5" /> Refresh
                </button>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm text-slate-300">
                  <thead className="bg-slate-900 text-slate-400 border-b border-slate-800 uppercase text-xs">
                    <tr>
                      <th className="p-3">Index</th>
                      <th className="p-3">Signal</th>
                      <th className="p-3">Strike</th>
                      <th className="p-3">Entry Premium</th>
                      <th className="p-3">SL</th>
                      <th className="p-3">Target</th>
                      <th className="p-3">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {signalsLog.map((sig, idx) => (
                      <tr key={sig._id || idx} className="border-b border-slate-800/50 hover:bg-slate-800/30">
                        <td className="p-3 font-semibold">{sig.index_name || 'NIFTY'}</td>
                        <td className={`p-3 font-bold ${sig.signal === 'BUY CALL' ? 'text-emerald-400' : 'text-red-400'}`}>{sig.signal}</td>
                        <td className="p-3 font-medium">{sig.strike}</td>
                        <td className="p-3 font-semibold text-cyan-400">₹{sig.entry_price}</td>
                        <td className="p-3 text-red-400">₹{sig.stop_loss}</td>
                        <td className="p-3 text-emerald-400">₹{sig.shz_upper}</td>
                        <td className="p-3">
                          <span className={`px-2.5 py-1 rounded text-xs font-bold ${sig.status === 'TARGET_HIT' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40' : sig.status === 'SL_HIT' ? 'bg-red-500/20 text-red-400 border border-red-500/40' : 'bg-indigo-500/20 text-indigo-400 border border-indigo-500/30'}`}>
                            {sig.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        ) : (
          /* 🟢 ADMIN PANEL VIEW */
          <div className="space-y-8">
            {/* Top Aggregate Stats Grid */}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <div className="bg-slate-900/80 border border-slate-800 p-6 rounded-2xl">
                <div className="flex justify-between items-center text-slate-400 mb-2">
                  <span className="text-xs font-bold uppercase">Total Users</span>
                  <Users className="w-5 h-5 text-indigo-400" />
                </div>
                <p className="text-3xl font-extrabold text-slate-100">{adminStats.total_users}</p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-6 rounded-2xl">
                <div className="flex justify-between items-center text-slate-400 mb-2">
                  <span className="text-xs font-bold uppercase">Today's Total Trades</span>
                  <Activity className="w-5 h-5 text-cyan-400" />
                </div>
                <p className="text-3xl font-extrabold text-cyan-400">{adminStats.total_trades}</p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-6 rounded-2xl">
                <div className="flex justify-between items-center text-slate-400 mb-2">
                  <span className="text-xs font-bold uppercase">Target Hit Count</span>
                  <Target className="w-5 h-5 text-emerald-400" />
                </div>
                <p className="text-3xl font-extrabold text-emerald-400">{adminStats.target_hits}</p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-6 rounded-2xl">
                <div className="flex justify-between items-center text-slate-400 mb-2">
                  <span className="text-xs font-bold uppercase">SL Hit Count</span>
                  <AlertTriangle className="w-5 h-5 text-red-400" />
                </div>
                <p className="text-3xl font-extrabold text-red-400">{adminStats.sl_hits}</p>
              </div>
            </div>

            {/* Users List & Individual User Trade Drilldown */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              {/* Users List */}
              <div className="bg-slate-900/60 border border-slate-800 p-6 rounded-3xl">
                <h3 className="text-lg font-bold text-slate-200 mb-4">Registered Users</h3>
                <div className="space-y-3 max-h-[500px] overflow-y-auto pr-2">
                  {usersList.map((u) => (
                    <div
                      key={u.id}
                      onClick={() => {
                        setSelectedAdminUser(u);
                        fetchUserTradesForAdmin(u.id);
                      }}
                      className={`p-4 rounded-2xl border cursor-pointer transition-all flex items-center justify-between ${selectedAdminUser?.id === u.id ? 'bg-indigo-600/20 border-indigo-500' : 'bg-slate-950/60 border-slate-800 hover:bg-slate-800/40'}`}
                    >
                      <div className="flex items-center gap-3">
                        <img src={u.picture} alt="" className="w-9 h-9 rounded-full border border-indigo-400/50" />
                        <div>
                          <p className="text-sm font-semibold text-slate-200">{u.full_name}</p>
                          <p className="text-xs text-slate-400">{u.email}</p>
                        </div>
                      </div>
                      <span className="text-xs text-indigo-400 font-semibold">View Trades →</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Selected User's Today Trade Logs */}
              <div className="md:col-span-2 bg-slate-900/60 border border-slate-800 p-6 rounded-3xl">
                <h3 className="text-lg font-bold text-slate-200 mb-4">
                  {selectedAdminUser ? `${selectedAdminUser.full_name}'s Today Trades` : 'Select a user to view today trades'}
                </h3>

                {selectedAdminUser ? (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm text-slate-300">
                      <thead className="bg-slate-900 text-slate-400 border-b border-slate-800 uppercase text-xs">
                        <tr>
                          <th className="p-3">Time</th>
                          <th className="p-3">Index</th>
                          <th className="p-3">Strike</th>
                          <th className="p-3">Entry</th>
                          <th className="p-3">SL</th>
                          <th className="p-3">Target</th>
                          <th className="p-3">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminUserTrades.length > 0 ? (
                          adminUserTrades.map((t, idx) => (
                            <tr key={t._id || idx} className="border-b border-slate-800/50">
                              <td className="p-3 text-slate-400 text-xs">{new Date(t.created_at).toLocaleTimeString()}</td>
                              <td className="p-3 font-semibold">{t.index_name}</td>
                              <td className="p-3 font-medium">{t.strike}</td>
                              <td className="p-3 text-cyan-400 font-semibold">₹{t.entry_price}</td>
                              <td className="p-3 text-red-400">₹{t.stop_loss}</td>
                              <td className="p-3 text-emerald-400">₹{t.shz_upper}</td>
                              <td className="p-3">
                                <span className={`px-2 py-0.5 rounded text-xs font-bold ${t.status === 'TARGET_HIT' ? 'bg-emerald-500/20 text-emerald-400' : t.status === 'SL_HIT' ? 'bg-red-500/20 text-red-400' : 'bg-indigo-500/20 text-indigo-400'}`}>
                                  {t.status}
                                </span>
                              </td>
                            </tr>
                          ))
                        ) : (
                          <tr>
                            <td colSpan="7" className="p-6 text-center text-slate-500 text-sm">
                              Is user ne aaj koi trade decode nahi kiya.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <div className="p-12 text-center text-slate-500">
                    ← Left menu se kisi user par click karein unka trade log dekhne ke liye.
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}