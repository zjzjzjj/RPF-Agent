import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ConfigProvider, App as AntApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "antd/dist/reset.css";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#29745a",
          colorInfo: "#29745a",
          colorSuccess: "#29745a",
          colorText: "#25332e",
          colorTextSecondary: "#78827d",
          colorBorder: "#dfe5e0",
          colorBgLayout: "#f5f6f3",
          borderRadius: 8,
          controlHeight: 36,
          fontFamily:
            '"Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif',
        },
        components: {
          Button: {
            primaryShadow: "0 3px 8px rgba(41, 116, 90, .12)",
            fontWeight: 500,
          },
          Card: { headerFontSize: 15, paddingLG: 22 },
          Table: {
            headerBg: "#f7f8f6",
            headerColor: "#78827d",
            headerSplitColor: "transparent",
            rowHoverBg: "#f5f9f5",
          },
          Menu: {
            darkItemBg: "#192b25",
            darkSubMenuItemBg: "#192b25",
            darkItemSelectedBg: "#30463c",
            darkItemSelectedColor: "#d3ebbc",
            darkItemHoverBg: "#243930",
            itemBorderRadius: 7,
            itemHeight: 44,
          },
          Layout: { bodyBg: "#f5f6f3", siderBg: "#192b25", headerBg: "#fff" },
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
