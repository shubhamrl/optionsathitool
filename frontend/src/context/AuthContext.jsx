import React, { createContext, useContext, useState, useEffect } from 'react';
import axios from 'axios';

const AuthContext = createContext();

// 🟢 Dynamic Backend API URL Configuration
const isLocal = typeof window !== 'undefined' && (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1');
const BACKEND_HOST = isLocal ? 'http://localhost:8000' : 'https://optionsathitool.onrender.com';
const API_BASE_URL = `${BACKEND_HOST}/api/v1`;

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [token, setToken] = useState(localStorage.getItem('optionsaathi_token') || null);
  const [loading, setLoading] = useState(true);
  const [serverWakingUp, setServerWakingUp] = useState(true);

  // 🟢 Ping Backend to handle Render Cold Start (Sleep mode wake up)
  const pingBackendHealth = async () => {
    let connected = false;
    while (!connected) {
      try {
        const res = await axios.get(`${BACKEND_HOST}/`, { timeout: 5000 });
        if (res.status === 200) {
          connected = true;
          setServerWakingUp(false);
        }
      } catch (err) {
        // Wait 3 seconds and retry until Render wakes up
        await new Promise((resolve) => setTimeout(resolve, 3000));
      }
    }
  };

  useEffect(() => {
    pingBackendHealth();
  }, []);

  useEffect(() => {
    if (token) {
      axios.defaults.headers.common['Authorization'] = `Bearer ${token}`;
      fetchUserProfile();
    } else {
      setLoading(false);
    }
  }, [token]);

  const fetchUserProfile = async () => {
    try {
      const res = await axios.get(`${API_BASE_URL}/auth/me`);
      if (res.data.success) {
        setUser(res.data.user);
      }
    } catch (err) {
      logout();
    } finally {
      setLoading(false);
    }
  };

  const loginWithGoogleToken = async (googleIdToken) => {
    try {
      const res = await axios.post(`${API_BASE_URL}/auth/google`, { id_token: googleIdToken });
      if (res.data.success) {
        const { access_token, user: userData } = res.data;
        localStorage.setItem('optionsaathi_token', access_token);
        axios.defaults.headers.common['Authorization'] = `Bearer ${access_token}`;
        setToken(access_token);
        setUser(userData);
        return { success: true };
      }
    } catch (err) {
      return { success: false, message: err.response?.data?.detail || 'Google Login Failed' };
    }
  };

  const logout = () => {
    localStorage.removeItem('optionsaathi_token');
    delete axios.defaults.headers.common['Authorization'];
    setToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, token, loading, serverWakingUp, loginWithGoogleToken, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);