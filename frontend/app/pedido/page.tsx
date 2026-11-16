import type { Metadata } from "next";

import "./pedido.css";

export const metadata: Metadata = {
  title: "Fazer pedido | Lojinha Klubi",
  description: "Área exclusiva para colaboradores realizarem pedidos na Lojinha Klubi.",
};

const FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSePCCz4JBSlRhAZUeLsV-RZsI6_0VgvUAESxAdUVToWZUaK7A/viewform?embedded=true";

export default function PedidoPage() {
  return (
    <main className="pedido-page">
      <section className="pedido-header" aria-labelledby="pedido-title">
        <img className="pedido-logo" src="/klubi-logo-black.png" alt="Klubi" />
        <div>
          <p className="pedido-eyebrow">LOJINHA KLUBI</p>
          <h1 id="pedido-title">Faça seu pedido</h1>
          <p className="pedido-description">Área exclusiva para colaboradores internos do Klubi.</p>
        </div>
      </section>

      <section className="pedido-access-note" aria-label="Orientação de acesso">
        <strong>Use seu e-mail corporativo</strong>
        <span>Se o Google solicitar login, entre com sua conta @klubi.com.br para acessar o formulário.</span>
      </section>

      <section className="pedido-form-card" aria-label="Formulário de pedido">
        <iframe
          title="Formulário de pedido da Lojinha Klubi"
          src={FORM_URL}
          className="pedido-form-frame"
          loading="eager"
        />
      </section>
    </main>
  );
}
