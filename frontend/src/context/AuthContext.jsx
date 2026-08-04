import React, { createContext, useContext, useState, useEffect } from 'react';
import axios from 'axios';

const AuthContext = createContext();

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [token, setToken] = useState(localStorage.getItem('optionsaathi_token') || null);
  const [loading, setLoading] = useState(true);

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
      const res = await axios.get('/api/v1/auth/me');
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
      const res = await axios.post('/api/v1/auth/google', { id_token: googleIdToken });
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
    <AuthContext.Provider value={{ user, token, loading, loginWithGoogleToken, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);