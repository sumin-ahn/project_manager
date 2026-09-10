import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { httpBridge } from "./api";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App bridge={httpBridge()} />
  </StrictMode>,
);
