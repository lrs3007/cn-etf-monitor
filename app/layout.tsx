import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "宽基研究台 · 因子与指数的长期观察",
  description: "五大A股宽基指数全历史因子监控、时间序列IC与样本外研究。",
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
