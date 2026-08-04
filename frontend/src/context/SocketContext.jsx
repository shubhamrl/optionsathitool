import React, { createContext, useContext, useEffect, useState } from 'react';

const SocketContext = createContext();

export const SocketProvider = ({ children, activeIndex = 'NIFTY' }) => {
  const [marketData, setMarketData] = useState(null);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    const wsUrl = `ws://localhost:8000/api/v1/market/ws/${activeIndex}`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      setIsConnected(true);
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'TICKER_STREAM') {
          setMarketData(message.data);
        }
      } catch (err) {}
    };

    ws.onclose = () => {
      setIsConnected(false);
    };

    return () => {
      ws.close();
    };
  }, [activeIndex]);

  return (
    <SocketContext.Provider value={{ marketData, isConnected }}>
      {children}
    </SocketContext.Provider>
  );
};

export const useMarketSocket = () => useContext(SocketContext);