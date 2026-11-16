"use client";

import { SignInButton, useAuth, useUser } from "@clerk/nextjs";
import { useEffect, useMemo, useState } from "react";

import "./loja.css";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
type AnyRecord = Record<string, any>;

const money = (value: number) => `R$ ${(Number(value) || 0).toFixed(2).replace(".", ",")}`;

async function api(path: string, token: string | null, options?: RequestInit) {
  const response = await fetch(`${API}${path}`, { ...options, credentials: "include", headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(options?.headers || {}) }, cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Não foi possível concluir a operação.");
  return data;
}

export default function LojaPage() {
  const { isLoaded, isSignedIn, getToken, signOut } = useAuth();
  const { user } = useUser();
  const [auth, setAuth] = useState<AnyRecord | null>(null);
  const [products, setProducts] = useState<AnyRecord[]>([]);
  const [orders, setOrders] = useState<AnyRecord[]>([]);
  const [quantities, setQuantities] = useState<Record<number, number>>({});
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState("");

  const load = async (token: string | null) => {
    setLoading(true);
    try {
      const [catalog, history] = await Promise.all([api("/loja/produtos", token), api("/loja/meus-pedidos", token)]);
      setAuth({ authenticated: true, user: { name: user?.fullName || user?.primaryEmailAddress?.emailAddress || "Colaborador" } });
      setProducts(catalog.items || []);
      setOrders(history.items || []);
      setMessage("");
    } catch (error) {
      setAuth({ authenticated: true, user: { name: user?.fullName || "Colaborador" } });
      setMessage(error instanceof Error ? error.message : "Não foi possível carregar a Lojinha.");
    } finally { setLoading(false); }
  };

  useEffect(() => {
    if (!isLoaded) return;
    if (!isSignedIn) { setAuth({ authenticated: false }); setLoading(false); return; }
    void getToken().then(load);
  }, [getToken, isLoaded, isSignedIn, user?.id]);

  const selectedItems = useMemo(() => Object.entries(quantities).filter(([, quantity]) => quantity > 0).map(([product_id, quantity]) => ({ product_id: Number(product_id), quantity })), [quantities]);
  const total = selectedItems.reduce((sum, item) => { const product = products.find(candidate => candidate.id === item.product_id); return sum + ((product?.unit_price || 0) * item.quantity); }, 0);

  const submit = async () => {
    if (!selectedItems.length || submitting) return;
    setSubmitting(true); setMessage("");
    try { const token = await getToken(); const result = await api("/loja/pedidos", token, { method: "POST", body: JSON.stringify({ items: selectedItems }) }); setQuantities({}); setMessage(`Pedido ${result.order_number} enviado para análise.`); await load(token); } catch (error) { setMessage(error instanceof Error ? error.message : "Não foi possível enviar o pedido."); } finally { setSubmitting(false); }
  };

  const logout = async () => { await signOut(); window.location.reload(); };

  if (loading) return <main className="loja-page"><div className="loja-loading">Carregando a Lojinha...</div></main>;
  if (!isSignedIn || !auth?.authenticated) return <main className="loja-page"><section className="loja-login"><img className="loja-logo" src="/klubi-logo-black.png" alt="Klubi" /><p className="loja-eyebrow">LOJINHA KLUBI</p><h1>Acesse a Lojinha</h1><p>Entre com seu e-mail corporativo @klubi.com.br para realizar pedidos.</p><SignInButton mode="modal"><button className="loja-primary">Entrar com Clerk</button></SignInButton><a className="loja-back" href="/">Voltar</a></section></main>;

  return <main className="loja-page"><header className="loja-topbar"><div><img className="loja-header-logo" src="/klubi-logo-black.png" alt="Klubi" /><p className="loja-eyebrow">LOJINHA KLUBI</p><h1>Olá, {auth.user?.name || "colaborador"}</h1><p>Escolha os produtos e envie seu pedido para aprovação.</p></div><button className="loja-secondary" onClick={logout}>Sair</button></header><section className="loja-layout"><div className="loja-catalog"><div className="loja-section-title"><h2>Produtos disponíveis</h2><span>{products.length} produto(s)</span></div>{products.length ? <div className="loja-products">{products.map(product => <article className="loja-product" key={product.id}><div><h3>{product.name}</h3><p>{product.size && `Tamanho: ${product.size} · `}{product.available_quantity} disponível(is)</p></div><strong>{money(product.unit_price)}</strong><input aria-label={`Quantidade de ${product.name}`} type="number" min={0} max={product.available_quantity} value={quantities[product.id] || 0} onChange={event => setQuantities({ ...quantities, [product.id]: Math.min(product.available_quantity, Math.max(0, Number(event.target.value) || 0)) })} /></article>)}</div> : <div className="loja-empty">Nenhum produto disponível no momento.</div>}</div><aside className="loja-cart"><div className="loja-section-title"><h2>Meu pedido</h2></div>{selectedItems.length ? <>{selectedItems.map(item => { const product = products.find(candidate => candidate.id === item.product_id); return <div className="loja-cart-line" key={item.product_id}><span>{item.quantity} × {product?.name}</span><strong>{money((product?.unit_price || 0) * item.quantity)}</strong></div>; })}<div className="loja-total"><span>Total</span><strong>{money(total)}</strong></div><button className="loja-primary" disabled={submitting} onClick={submit}>{submitting ? "Enviando..." : "Enviar pedido"}</button><small>O estoque será baixado somente após a aprovação.</small></> : <p className="loja-muted">Selecione produtos para montar seu pedido.</p>}</aside></section><section className="loja-orders"><div className="loja-section-title"><h2>Meus pedidos</h2></div>{orders.length ? orders.map(order => <div className="loja-order" key={order.id}><strong>{order.order_number}</strong><span>{new Date(order.created_at).toLocaleString("pt-BR")}</span><span className={`loja-status ${order.status.toLowerCase()}`}>{order.status}</span><strong>{money(order.total)}</strong></div>) : <p className="loja-muted">Você ainda não fez pedidos.</p>}</section>{message && <div className="loja-message" role="status">{message}</div>}</main>;
}
