import type { Metadata } from "next";
import { ClerkProvider } from "@clerk/nextjs";
import "./globals.css";

export const metadata: Metadata = {
  title: "LOJINHA KLUBI",
  description: "Gestão de pedidos, estoque e fechamento da Lojinha Klubi",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <ClerkProvider><html lang="pt-BR"><body>{children}</body></html></ClerkProvider>;
}
