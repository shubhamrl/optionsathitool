import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { useAuth } from '../context/AuthContext';
import { GoogleLogin } from '@react-oauth/google';
import { Activity, Zap, LogOut, TrendingUp, RefreshCw } from 'lucide-react';

const INDICES = ['NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY'];

// 🟢 Explicit Production & Local API Base URLs (No double /api/v1 prefixing)
const isLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

const BACKEND_HOST = isLocal 
  ? 'http://localhost:8000' 
  : 'https://optionsathitool.onrender.com';

const API_BASE_URL = `${BACKEND_HOST}/api/v1`;

const WS_BASE_URL = isLocal 
  ? 'ws://localhost:8000' 
  : 'wss://optionsathitool.onrender.com';
export default function Dashboard() {
  const { user, loginWithGoogleToken, logout } = useAuth();
  const [selectedIndex, setSelectedIndex] = useState('NIFTY');
  const [liveSpot, setLiveSpot] = useState(0.0);
  const [pcr, setPcr] = useState(1.0);
  const [regime, setRegime] = useState('NEUTRAL');
  const [activeSignal, setActiveSignal] = useState(null);
  const [signalsLog, setSignalsLog] = useState([]);
  const [loadingDecode, setLoadingDecode] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  
  const wsRef = useRef(null);

  // 🟢 Connect Real-time App WebSocket for Selected Index
  useEffect(() => {
    connectWebSocket(selectedIndex);
    fetchSignalsLog();

    return () => {
      if (wsRef.current) wsRef.current.close();
    };
  }, [selectedIndex]);

 const connectWebSocket = (indexName) => {
    if (wsRef.current) wsRef.current.close();

    // 🟢 Uses Dynamic WS URL
    const wsUrl = `${WS_BASE_URL}/api/v1/market/ws/${indexName}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => setWsConnected(true);
    
    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'TICKER_STREAM') {
          const indexStore = message.data || {};
          // Update Live Spot
          if (indexStore.spot && indexStore.spot > 0) {
            setLiveSpot(indexStore.spot);
          }
        } else if (message.type === 'SIGNAL_STATUS_UPDATE') {
          // Live status update for active trade
          fetchSignalsLog();
        }
      } catch (e) {}
    };

    ws.onclose = () => setWsConnected(false);
  };

 const fetchSignalsLog = async () => {
    try {
      const res = await axios.get(`${API_BASE_URL}/signals/automated-signals-log`);
      if (res.data.success) {
        setSignalsLog(res.data.logs);
      }
    } catch (e) {
      console.error("Signals Log Fetch Error:", e);
    }
  };

  const handleDecodeSignal = async (isForce = false) => {
    setLoadingDecode(true);
    try {
      const endpoint = isForce ? `${API_BASE_URL}/signals/decode-force` : `${API_BASE_URL}/signals/decode`;
      const res = await axios.post(endpoint, { index_name: selectedIndex });
      if (res.data.success) {
        setActiveSignal(res.data.data);
        fetchSignalsLog();
      }
    } catch (e) {
      console.error("Decode Signal Error:", e);
    } finally {
      setLoadingDecode(false);
    }
  };

  const handleDecodeSignal = async (isForce = false) => {
    setLoadingDecode(true);
    try {
      const endpoint = isForce ? `${API_BASE_URL}/signals/decode-force` : `${API_BASE_URL}/signals/decode`;
      const res = await axios.post(endpoint, { index_name: selectedIndex });
      if (res.data.success) {
        setActiveSignal(res.data.data);
        fetchSignalsLog();
      }
    } catch (e) {
    } finally {
      setLoadingDecode(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans">
      {/* Top Header */}
      <header className="border-b border-slate-800 bg-slate-900/50 px-6 py-4 flex justify-between items-center backdrop-blur-md">
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

        <div>
          {user ? (
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
          ) : (
            <GoogleLogin
              onSuccess={(credentialResponse) => loginWithGoogleToken(credentialResponse.credential)}
              onError={() => console.log('Google Login Failed')}
            />
          )}
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8">
        {/* Multi-Index Selector Tabs */}
        <div className="flex gap-3 mb-8 bg-slate-900/80 p-1.5 rounded-2xl border border-slate-800 w-fit">
          {INDICES.map((idx) => (
            <button
              key={idx}
              onClick={() => setSelectedIndex(idx)}
              className={`px-6 py-2.5 rounded-xl text-sm font-semibold transition-all ${
                selectedIndex === idx
                  ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
              }`}
            >
              {idx}
            </button>
          ))}
        </div>

        {/* 🟢 Live Market Ticker Grid With Flashing Spot Numbers */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
          <div className="bg-slate-900/60 border border-slate-800 p-5 rounded-2xl relative overflow-hidden">
            <p className="text-xs font-medium text-slate-400 mb-1">{selectedIndex} SPOT PRICE</p>
            <p className="text-3xl font-extrabold text-cyan-400 tracking-tight transition-all">
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
                disabled={loadingDecode || !user}
                onClick={() => handleDecodeSignal(false)}
                className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white font-semibold px-6 py-3 rounded-xl transition-all shadow-lg shadow-indigo-600/30"
              >
                {loadingDecode ? 'Scanning Market...' : 'DECODE SIGNAL'}
              </button>
              <button
                disabled={loadingDecode || !user}
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

        {/* 🟢 Live Trade Logs Table with Real-time Status Locks */}
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
                {signalsLog.map((sig) => (
                  <tr key={sig._id} className="border-b border-slate-800/50 hover:bg-slate-800/30">
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
      </main>
    </div>
  );
}