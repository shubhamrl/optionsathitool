import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { useAuth } from '../context/AuthContext';
import { GoogleLogin } from '@react-oauth/google';
import { Activity, Zap, LogOut, RefreshCw, ShieldCheck, Users, Target, AlertTriangle, Wallet, Layers, TrendingUp, TrendingDown, XCircle } from 'lucide-react';

const INDICES = ['NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY'];

const isLocal = typeof window !== 'undefined' && (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1');
const BACKEND_HOST = isLocal ? 'http://localhost:8000' : 'https://optionsathitool.onrender.com';
const API_BASE_URL = `${BACKEND_HOST}/api/v1`;
const WS_BASE_URL = isLocal ? 'ws://localhost:8000' : 'wss://optionsathitool.onrender.com';

export default function Dashboard() {
  const { user, serverWakingUp, loginWithGoogleToken, logout } = useAuth();
  const [activeTab, setActiveTab] = useState('TRADING');
  const [selectedIndex, setSelectedIndex] = useState('NIFTY');
  const [liveSpot, setLiveSpot] = useState(0.0);
  const [pcr, setPcr] = useState(1.0);
  const [regime, setRegime] = useState('NEUTRAL');
  const [activeSignal, setActiveSignal] = useState(null);
  const [signalsLog, setSignalsLog] = useState([]);
  const [loadingDecode, setLoadingDecode] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [showProfileMenu, setShowProfileMenu] = useState(false);

  // Paper Trading States
  const [paperWallet, setPaperWallet] = useState({ balance: 100000, realized_pnl: 0, total_taxes_paid: 0 });
  const [paperPositions, setPaperPositions] = useState([]);
  const [paperLots, setPaperLots] = useState(1);
  const [placingPaperTrade, setPlacingPaperTrade] = useState(false);
  const [exitingAll, setExitingAll] = useState(false);

  // Admin States
  const [adminStats, setAdminStats] = useState({ total_users: 0, total_trades: 0, target_hits: 0, sl_hits: 0 });
  const [usersList, setUsersList] = useState([]);
  const [selectedAdminUser, setSelectedAdminUser] = useState(null);
  const [adminUserTrades, setAdminUserTrades] = useState([]);

  const wsRef = useRef(null);

  const fetchSignalsLog = async () => {
    try {
      const res = await axios.get(`${API_BASE_URL}/signals/automated-signals-log`);
      if (res.data && res.data.success) setSignalsLog(res.data.logs || []);
    } catch (e) {}
  };

  const fetchPaperData = async () => {
    try {
      const [walletRes, posRes] = await Promise.all([
        axios.get(`${API_BASE_URL}/paper/wallet`),
        axios.get(`${API_BASE_URL}/paper/positions`)
      ]);
      if (walletRes.data.success) setPaperWallet(walletRes.data.wallet);
      if (posRes.data.success) setPaperPositions(posRes.data.positions);
    } catch (e) {}
  };

  const fetchAdminData = async () => {
    try {
      const [statsRes, usersRes] = await Promise.all([
        axios.get(`${API_BASE_URL}/signals/admin/today-stats`),
        axios.get(`${API_BASE_URL}/auth/admin/users-list`)
      ]);
      if (statsRes.data.success) setAdminStats(statsRes.data.stats);
      if (usersRes.data.success) setUsersList(usersRes.data.users);
    } catch (e) {}
  };

  const fetchUserTradesForAdmin = async (userId) => {
    try {
      const res = await axios.get(`${API_BASE_URL}/signals/admin/user-trades/${userId}`);
      if (res.data.success) setAdminUserTrades(res.data.trades);
    } catch (e) {}
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
          if (indexStore.spot && indexStore.spot > 0) setLiveSpot(indexStore.spot);
        } else if (message.type === 'SIGNAL_STATUS_UPDATE') {
          fetchSignalsLog();
          fetchPaperData();
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
      fetchPaperData();
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
    } catch (e) {} finally { setLoadingDecode(false); }
  };

  const handleExecutePaperTrade = async () => {
    if (!activeSignal) return;
    setPlacingPaperTrade(true);
    try {
      const res = await axios.post(`${API_BASE_URL}/paper/place-trade`, {
        index_name: selectedIndex,
        signal: activeSignal.signal,
        strike: activeSignal.strike,
        entry_price: activeSignal.entry_price,
        stop_loss: activeSignal.stop_loss,
        target1: activeSignal.shz_upper,
        lots: paperLots
      });
      if (res.data && res.data.success) {
        alert("🎉 Paper Order Executed Successfully!");
        fetchPaperData();
      }
    } catch (e) {
      alert(e.response?.data?.detail || "Execution failed!");
    } finally { setPlacingPaperTrade(false); }
  };

  const handleSquareOff = async (tradeId, currentLtp) => {
    try {
      const res = await axios.post(`${API_BASE_URL}/paper/square-off/${tradeId}`, { exit_price: currentLtp });
      if (res.data && res.data.success) {
        alert(`Position Squared Off! Net PnL: ₹${res.data.net_pnl}`);
        fetchPaperData();
      }
    } catch (e) {
      alert("Square off failed!");
    }
  };

  const handleExitAllPositions = async () => {
  const openPositions = paperPositions.filter(p => p.status === 'OPEN');
  if (openPositions.length === 0) {
    alert("Koi Open Position nahi hai!");
    return;
  }

  if (!window.confirm(`Kya aap saari (${openPositions.length}) open positions ek saath close karna chahte hain?`)) {
    return;
  }

  setExitingAll(true);
  try {
    const pricesMap = {};
    openPositions.forEach(p => {
      const { currentLtp } = getLivePositionMetrics(p);
      pricesMap[p._id] = currentLtp;
    });

    const res = await axios.post(`${API_BASE_URL}/paper/square-off-all`, { prices_map: pricesMap });
    if (res.data && res.data.success) {
      alert(`🎉 ${res.data.message}\nTotal Net PnL: ₹${res.data.total_net_pnl}`);
      fetchPaperData();
    }
  } catch (e) {
    alert("Failed to exit all positions!");
  } finally {
    setExitingAll(false);
  }
};

const openPositionsList = paperPositions.filter(p => p.status === 'OPEN');

  const getLivePositionMetrics = (p) => {
    if (p.status !== 'OPEN') {
      return { currentLtp: p.sell_price || p.buy_price, pnl: p.net_pnl || 0 };
    }
    const simulatedLtp = +(p.buy_price * (1 + (liveSpot > 0 ? ((liveSpot % 10) - 5) / 500 : 0))).toFixed(2);
    const grossPnl = +((simulatedLtp - p.buy_price) * p.quantity).toFixed(2);
    return { currentLtp: simulatedLtp, pnl: grossPnl };
  };

  const totalUnrealizedPnl = paperPositions
    .filter(p => p.status === 'OPEN')
    .reduce((acc, p) => acc + getLivePositionMetrics(p).pnl, 0);

  if (serverWakingUp) {
    return (
      <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center p-6 text-center">
        <div className="relative mb-6">
          <div className="w-20 h-20 rounded-full border-4 border-indigo-500/20 border-t-indigo-500 animate-spin"></div>
          <Zap className="w-8 h-8 text-indigo-400 absolute inset-0 m-auto" />
        </div>
        <h2 className="text-xl md:text-2xl font-bold text-slate-100">Waking Up OptionSaathi AI Engine...</h2>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="min-h-screen bg-slate-950 flex flex-col justify-between items-center p-4 md:p-6">
        <div className="max-w-md w-full bg-slate-900/80 border border-slate-800 rounded-3xl p-6 text-center shadow-2xl my-auto">
          <Activity className="w-10 h-10 text-indigo-400 mx-auto mb-4" />
          <h2 className="text-xl font-bold text-slate-100 mb-4">Access OptionSaathi AI</h2>
          <GoogleLogin onSuccess={(res) => loginWithGoogleToken(res.credential)} />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans">
      <header className="border-b border-slate-800 bg-slate-900/50 px-4 md:px-6 py-3 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <div className="w-9 h-9 rounded-xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center">
              <Zap className="text-indigo-400 w-5 h-5" />
            </div>
            <span className="text-base md:text-xl font-bold bg-gradient-to-r from-indigo-400 to-cyan-400 bg-clip-text text-transparent">
              OptionSaathi
            </span>
          </div>

          <div className="flex bg-slate-950 p-1 rounded-xl border border-slate-800">
            <button onClick={() => setActiveTab('TRADING')} className={`px-3 py-1 rounded-lg text-xs font-bold ${activeTab === 'TRADING' ? 'bg-indigo-600 text-white' : 'text-slate-400'}`}>
              TRADING
            </button>
            <button onClick={() => setActiveTab('PAPER')} className={`px-3 py-1 rounded-lg text-xs font-bold flex items-center gap-1 ${activeTab === 'PAPER' ? 'bg-indigo-600 text-white' : 'text-slate-400'}`}>
              <Wallet className="w-3 h-3 text-cyan-400" /> PAPER
            </button>
            {user.role === 'admin' && (
              <button onClick={() => setActiveTab('ADMIN')} className={`px-3 py-1 rounded-lg text-xs font-bold flex items-center gap-1 ${activeTab === 'ADMIN' ? 'bg-indigo-600 text-white' : 'text-slate-400'}`}>
                <ShieldCheck className="w-3 h-3 text-amber-400" /> ADMIN
              </button>
            )}
          </div>

          <div className="relative">
            <button onClick={() => setShowProfileMenu(!showProfileMenu)}>
              <img src={user.picture} alt="" className="w-8 h-8 rounded-full border-2 border-indigo-500/60 p-0.5" />
            </button>
            {showProfileMenu && (
              <div className="absolute right-0 mt-2 w-56 bg-slate-900 border border-slate-800 rounded-2xl p-4 shadow-2xl z-50">
                <p className="text-sm font-semibold truncate">{user.full_name}</p>
                <p className="text-xs text-slate-400 truncate mb-3">{user.email}</p>
                <button onClick={logout} className="w-full flex items-center justify-center gap-2 bg-red-500/10 text-red-400 border border-red-500/30 py-1.5 rounded-xl text-xs font-semibold">
                  <LogOut className="w-3.5 h-3.5" /> Logout
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 md:px-6 py-6">
        {activeTab === 'TRADING' && (
          <>
            <div className="flex gap-2 mb-6 bg-slate-900/80 p-1.5 rounded-2xl border border-slate-800 overflow-x-auto no-scrollbar">
              {INDICES.map((idx) => (
                <button key={idx} onClick={() => setSelectedIndex(idx)} className={`px-5 py-2 rounded-xl text-xs font-semibold transition-all ${selectedIndex === idx ? 'bg-indigo-600 text-white' : 'text-slate-400'}`}>
                  {idx}
                </button>
              ))}
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
              <div className="bg-slate-900/60 border border-slate-800 p-4 rounded-2xl">
                <p className="text-xs text-slate-400 mb-1">{selectedIndex} SPOT</p>
                <p className="text-2xl font-extrabold text-cyan-400">₹{liveSpot > 0 ? liveSpot.toLocaleString('en-IN') : 'Loading...'}</p>
              </div>
              <div className="bg-slate-900/60 border border-slate-800 p-4 rounded-2xl">
                <p className="text-xs text-slate-400 mb-1">PCR SENTIMENT</p>
                <p className="text-2xl font-bold text-indigo-400">{pcr}</p>
              </div>
              <div className="bg-slate-900/60 border border-slate-800 p-4 rounded-2xl">
                <p className="text-xs text-slate-400 mb-1">MARKET REGIME</p>
                <p className="text-xl font-bold text-emerald-400">{regime}</p>
              </div>
              <div className="bg-slate-900/60 border border-slate-800 p-4 rounded-2xl">
                <p className="text-xs text-slate-400 mb-1">INDIA VIX</p>
                <p className="text-2xl font-bold text-amber-400">13.5</p>
              </div>
            </div>

            <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 mb-6">
              <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-4">
                <div>
                  <h2 className="text-xl font-bold">AI Confluence Engine</h2>
                  <p className="text-xs text-slate-400 mt-0.5">Real-time Analytics & Automated Matrix</p>
                </div>
                <div className="flex gap-2 w-full sm:w-auto">
                  <button onClick={() => handleDecodeSignal(false)} disabled={loadingDecode} className="flex-1 sm:flex-none bg-indigo-600 text-white font-semibold px-5 py-2.5 rounded-xl text-xs">
                    {loadingDecode ? 'Scanning...' : 'DECODE SIGNAL'}
                  </button>
                  <button onClick={() => handleDecodeSignal(true)} disabled={loadingDecode} className="flex-1 sm:flex-none bg-amber-600/20 text-amber-300 border border-amber-500/40 px-5 py-2.5 rounded-xl text-xs font-semibold">
                    FORCED SCALP
                  </button>
                </div>
              </div>

              {activeSignal && (
                <div className="bg-slate-950/80 border border-slate-800 p-5 rounded-2xl mt-4">
                  <div className="flex justify-between items-start mb-4">
                    <div>
                      <span className="px-2.5 py-0.5 rounded-full text-xs font-bold bg-indigo-500/20 text-indigo-400">{activeSignal.signal}</span>
                      <h3 className="text-2xl font-bold mt-1">{activeSignal.strike}</h3>
                    </div>
                    <div className="text-right">
                      <p className="text-xs text-slate-400">ENTRY PREMIUM</p>
                      <p className="text-2xl font-extrabold text-cyan-400">₹{activeSignal.entry_price}</p>
                    </div>
                  </div>

                  <div className="mt-4 pt-4 border-t border-slate-800 flex flex-col sm:flex-row items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-slate-400">Lots:</span>
                      {[1, 2, 5, 10].map((l) => (
                        <button key={l} onClick={() => setPaperLots(l)} className={`px-3 py-1 rounded-lg text-xs font-bold ${paperLots === l ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300'}`}>
                          {l} L
                        </button>
                      ))}
                    </div>
                    <button onClick={handleExecutePaperTrade} disabled={placingPaperTrade} className="w-full sm:w-auto bg-gradient-to-r from-emerald-500 to-teal-600 text-slate-950 font-extrabold px-6 py-2.5 rounded-xl text-xs flex items-center justify-center gap-2">
                      <Wallet className="w-4 h-4" /> {placingPaperTrade ? 'Executing...' : 'EXECUTE PAPER TRADE'}
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Today's Trade Logs Table */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-3xl p-4 md:p-6">
              <div className="flex justify-between items-center mb-4">
                <h3 className="text-sm md:text-lg font-bold text-slate-200">Today's Automated Trade Logs</h3>
                <button onClick={fetchSignalsLog} className="text-slate-400 hover:text-indigo-400 flex items-center gap-1 text-xs">
                  <RefreshCw className="w-3.5 h-3.5" /> Refresh
                </button>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs md:text-sm text-slate-300 min-w-[500px]">
                  <thead className="bg-slate-900 text-slate-400 border-b border-slate-800 uppercase text-[10px] md:text-xs">
                    <tr>
                      <th className="p-2.5">Index</th>
                      <th className="p-2.5">Signal</th>
                      <th className="p-2.5">Strike</th>
                      <th className="p-2.5">Entry</th>
                      <th className="p-2.5">SL</th>
                      <th className="p-2.5">Target</th>
                      <th className="p-2.5">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {signalsLog.length > 0 ? (
                      signalsLog.map((sig, idx) => (
                        <tr key={sig._id || idx} className="border-b border-slate-800/50 hover:bg-slate-800/30">
                          <td className="p-2.5 font-semibold">{sig.index_name || 'NIFTY'}</td>
                          <td className={`p-2.5 font-bold ${sig.signal === 'BUY CALL' ? 'text-emerald-400' : 'text-red-400'}`}>{sig.signal}</td>
                          <td className="p-2.5 font-medium">{sig.strike}</td>
                          <td className="p-2.5 font-semibold text-cyan-400">₹{sig.entry_price}</td>
                          <td className="p-2.5 text-red-400">₹{sig.stop_loss}</td>
                          <td className="p-2.5 text-emerald-400">₹{sig.shz_upper}</td>
                          <td className="p-2.5">
                            <span className={`px-2 py-0.5 rounded text-[10px] md:text-xs font-bold ${sig.status === 'TARGET_HIT' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40' : sig.status === 'SL_HIT' ? 'bg-red-500/20 text-red-400 border border-red-500/40' : 'bg-indigo-500/20 text-indigo-400 border border-indigo-500/30'}`}>
                              {sig.status}
                            </span>
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan="7" className="p-6 text-center text-slate-500 text-xs">
                          Aaj ke liye koi automated trade log nahi hai. 'DECODE SIGNAL' button dabayein!
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}

        {activeTab === 'PAPER' && (
          <div className="space-y-6">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-slate-900/80 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-bold text-slate-400 uppercase mb-1">Available Capital</p>
                <p className="text-2xl font-extrabold text-cyan-400">₹{paperWallet.balance.toLocaleString('en-IN')}</p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-bold text-slate-400 uppercase mb-1">Live Open MTM PnL</p>
                <p className={`text-2xl font-extrabold flex items-center gap-1 ${totalUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {totalUnrealizedPnl >= 0 ? <TrendingUp className="w-5 h-5" /> : <TrendingDown className="w-5 h-5" />}
                  ₹{totalUnrealizedPnl.toLocaleString('en-IN')}
                </p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-bold text-slate-400 uppercase mb-1">Realized Net PnL</p>
                <p className={`text-2xl font-extrabold ${paperWallet.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  ₹{paperWallet.realized_pnl.toLocaleString('en-IN')}
                </p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-5 rounded-2xl">
                <p className="text-xs font-bold text-slate-400 uppercase mb-1">Total Charges Paid</p>
                <p className="text-2xl font-extrabold text-amber-400">₹{paperWallet.total_taxes_paid}</p>
              </div>
            </div>

            <div className="bg-slate-900/60 border border-slate-800 rounded-3xl p-5">
             <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-4">
  <h3 className="text-base font-bold text-slate-200 flex items-center gap-2">
    <Layers className="w-5 h-5 text-cyan-400" /> Active & Closed Paper Positions
  </h3>
  
  {/* Bulk Square-off Button */}
  {openPositionsList.length > 0 && (
    <button
      disabled={exitingAll}
      onClick={handleExitAllPositions}
      className="w-full sm:w-auto bg-red-600/20 hover:bg-red-600/30 text-red-400 border border-red-500/50 px-4 py-2 rounded-xl text-xs font-extrabold flex items-center justify-center gap-2 transition-all"
    >
      {exitingAll ? 'Closing All...' : `EXIT ALL POSITIONS (${openPositionsList.length})`}
    </button>
  )}
</div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs md:text-sm text-slate-300 min-w-[700px]">
                  <thead className="bg-slate-900 text-slate-400 border-b border-slate-800 uppercase text-[10px]">
                    <tr>
                      <th className="p-3">Index</th>
                      <th className="p-3">Strike</th>
                      <th className="p-3">Qty / Lots</th>
                      <th className="p-3">Buy Price</th>
                      <th className="p-3">Live LTP</th>
                      <th className="p-3">Live MTM PnL</th>
                      <th className="p-3">Status</th>
                      <th className="p-3 text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paperPositions.length > 0 ? (
                      paperPositions.map((p, idx) => {
                        const { currentLtp, pnl } = getLivePositionMetrics(p);
                        return (
                          <tr key={p._id || idx} className="border-b border-slate-800/50 hover:bg-slate-800/30">
                            <td className="p-3 font-semibold">{p.index_name}</td>
                            <td className="p-3 font-medium">{p.strike}</td>
                            <td className="p-3 text-slate-400">{p.quantity} ({p.lots} L)</td>
                            <td className="p-3 font-semibold text-cyan-400">₹{p.buy_price}</td>
                            <td className="p-3 font-bold text-slate-100 animate-pulse">₹{currentLtp}</td>
                            <td className={`p-3 font-extrabold ${pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                              {pnl >= 0 ? `+₹${pnl}` : `-₹${Math.abs(pnl)}`}
                            </td>
                            <td className="p-3">
                              <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${p.status === 'OPEN' ? 'bg-indigo-500/20 text-indigo-400 border border-indigo-500/40' : p.status === 'TARGET_HIT' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-red-500/20 text-red-400'}`}>
                                {p.status}
                              </span>
                            </td>
                            <td className="p-3 text-right">
                              {p.status === 'OPEN' && (
                                <button onClick={() => handleSquareOff(p._id, currentLtp)} className="bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/40 px-3 py-1 rounded-lg text-xs font-bold transition-all flex items-center gap-1 ml-auto">
                                  <XCircle className="w-3.5 h-3.5" /> Exit
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })
                    ) : (
                      <tr>
                        <td colSpan="8" className="p-8 text-center text-slate-500 text-xs">
                          Koi Active Position nahi hai. TRADING tab se Paper Trade execute karein!
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {activeTab === 'ADMIN' && (
          <div className="space-y-6">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
              <div className="bg-slate-900/80 border border-slate-800 p-4 md:p-6 rounded-2xl">
                <p className="text-[10px] md:text-xs font-bold text-slate-400 uppercase mb-1">Total Users</p>
                <p className="text-xl md:text-3xl font-extrabold text-slate-100">{adminStats.total_users}</p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-4 md:p-6 rounded-2xl">
                <p className="text-[10px] md:text-xs font-bold text-slate-400 uppercase mb-1">Total Trades</p>
                <p className="text-xl md:text-3xl font-extrabold text-cyan-400">{adminStats.total_trades}</p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-4 md:p-6 rounded-2xl">
                <p className="text-[10px] md:text-xs font-bold text-slate-400 uppercase mb-1">Target Hits</p>
                <p className="text-xl md:text-3xl font-extrabold text-emerald-400">{adminStats.target_hits}</p>
              </div>
              <div className="bg-slate-900/80 border border-slate-800 p-4 md:p-6 rounded-2xl">
                <p className="text-[10px] md:text-xs font-bold text-slate-400 uppercase mb-1">SL Hits</p>
                <p className="text-xl md:text-3xl font-extrabold text-red-400">{adminStats.sl_hits}</p>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <div className="bg-slate-900/60 border border-slate-800 p-4 md:p-6 rounded-3xl">
                <h3 className="text-base md:text-lg font-bold text-slate-200 mb-4">Registered Users</h3>
                <div className="space-y-2.5 max-h-[400px] overflow-y-auto pr-1">
                  {usersList.map((u) => (
                    <div
                      key={u.id}
                      onClick={() => {
                        setSelectedAdminUser(u);
                        fetchUserTradesForAdmin(u.id);
                      }}
                      className={`p-3 rounded-2xl border cursor-pointer transition-all flex items-center justify-between ${selectedAdminUser?.id === u.id ? 'bg-indigo-600/20 border-indigo-500' : 'bg-slate-950/60 border-slate-800 hover:bg-slate-800/40'}`}
                    >
                      <div className="flex items-center gap-2.5 overflow-hidden">
                        <img src={u.picture} alt="" className="w-8 h-8 rounded-full border border-indigo-400/50 shrink-0" />
                        <div className="overflow-hidden">
                          <p className="text-xs md:text-sm font-semibold text-slate-200 truncate">{u.full_name}</p>
                          <p className="text-[10px] text-slate-400 truncate">{u.email}</p>
                        </div>
                      </div>
                      <span className="text-[10px] text-indigo-400 font-semibold shrink-0">Trades →</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="md:col-span-2 bg-slate-900/60 border border-slate-800 p-4 md:p-6 rounded-3xl">
                <h3 className="text-base md:text-lg font-bold text-slate-200 mb-4">
                  {selectedAdminUser ? `${selectedAdminUser.full_name}'s Trades` : 'Select a user'}
                </h3>

                {selectedAdminUser ? (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs md:text-sm text-slate-300 min-w-[500px]">
                      <thead className="bg-slate-900 text-slate-400 border-b border-slate-800 uppercase text-[10px]">
                        <tr>
                          <th className="p-2.5">Time</th>
                          <th className="p-2.5">Index</th>
                          <th className="p-2.5">Strike</th>
                          <th className="p-2.5">Entry</th>
                          <th className="p-2.5">SL</th>
                          <th className="p-2.5">Target</th>
                          <th className="p-2.5">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminUserTrades.length > 0 ? (
                          adminUserTrades.map((t, idx) => (
                            <tr key={t._id || idx} className="border-b border-slate-800/50">
                              <td className="p-2.5 text-slate-400 text-[10px]">{new Date(t.created_at).toLocaleTimeString()}</td>
                              <td className="p-2.5 font-semibold">{t.index_name}</td>
                              <td className="p-2.5 font-medium">{t.strike}</td>
                              <td className="p-2.5 text-cyan-400 font-semibold">₹{t.entry_price}</td>
                              <td className="p-2.5 text-red-400">₹{t.stop_loss}</td>
                              <td className="p-2.5 text-emerald-400">₹{t.shz_upper}</td>
                              <td className="p-2.5">
                                <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${t.status === 'TARGET_HIT' ? 'bg-emerald-500/20 text-emerald-400' : t.status === 'SL_HIT' ? 'bg-red-500/20 text-red-400' : 'bg-indigo-500/20 text-indigo-400'}`}>
                                  {t.status}
                                </span>
                              </td>
                            </tr>
                          ))
                        ) : (
                          <tr>
                            <td colSpan="7" className="p-6 text-center text-slate-500 text-xs">
                              Is user ne aaj koi trade decode nahi kiya.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <div className="p-8 text-center text-slate-500 text-xs">
                    ← Left list se kisi user par click karein unka trade log dekhne ke liye.
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