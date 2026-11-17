"use client";

import { SignInButton, useAuth, useUser } from "@clerk/nextjs";
import { useEffect, useMemo, useState } from "react";

import "./loja.css";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const CART_KEY = "lojinha-klubi-carrinho";
type AnyRecord = Record<string, any>;
type Variant = { id: number; base_name: string; size: string; unit_price: number; available_quantity: number };
type Group = { key: string; name: string; description: string; image_url: string | null; sized: boolean; variants: Variant[] };
type Cart = Record<number, number>;

const money = (value: number) => (Number(value) || 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const cents = (value: number) => Math.round((Number(value) || 0) * 100);
const normalize = (value: string) => value.normalize("NFD").replace(/[̀-ͯ]/g, "").trim().toUpperCase();
const statusLabel: Record<string, string> = { PENDENTE: "Aguardando aprovação", APROVADO: "Aprovado", CONCLUIDO: "Entregue", CANCELADO: "Cancelado" };

// Ordem natural dos tamanhos de roupa; tamanhos desconhecidos vão para o fim, números em ordem crescente.
const SIZE_ORDER = ["PP", "P", "M", "G", "GG", "XG", "XGG", "EX1", "EX2", "EX3", "EXG", "XG1", "XG2", "XG3", "G1", "G2", "G3", "G4"];
const isSingleSize = (size?: string | null) => !size || ["UNICO", "U", "TAMANHO UNICO"].includes(normalize(size));
function compareSizes(a: string, b: string) {
  const rank = (size: string) => { const index = SIZE_ORDER.indexOf(normalize(size)); return index < 0 ? 100 : index; };
  return rank(a) - rank(b) || (Number(a) || 0) - (Number(b) || 0) || a.localeCompare(b, "pt-BR");
}

function groupProducts(items: AnyRecord[]): Group[] {
  const groups = new Map<string, Group>();
  for (const item of items) {
    const name = item.group_name || item.base_name || item.name;
    const key = normalize(name);
    const group: Group = groups.get(key) || { key, name, description: "", image_url: null, sized: false, variants: [] };
    group.description ||= item.description || "";
    group.image_url ||= item.image_url || null;
    group.variants.push({ id: item.id, base_name: group.name, size: isSingleSize(item.size) ? "" : String(item.size).trim(), unit_price: Number(item.unit_price) || 0, available_quantity: Number(item.available_quantity) || 0 });
    groups.set(key, group);
  }
  for (const group of groups.values()) {
    group.sized = group.variants.some(variant => variant.size);
    group.variants.sort((a, b) => compareSizes(a.size, b.size));
  }
  return [...groups.values()];
}

// Mantém no carrinho só o que ainda existe e cabe no estoque disponível.
function sanitizeCart(cart: Cart, items: AnyRecord[]): Cart {
  const available = new Map(items.map(item => [Number(item.id), Number(item.available_quantity) || 0]));
  const clean: Cart = {};
  for (const [id, quantity] of Object.entries(cart)) {
    const limit = available.get(Number(id)) || 0;
    const value = Math.min(Number(quantity) || 0, limit);
    if (value > 0) clean[Number(id)] = value;
  }
  return clean;
}

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
  const [cart, setCart] = useState<Cart>({});
  const [cartReady, setCartReady] = useState(false);
  const [selected, setSelected] = useState<Record<string, number>>({});
  const [sizeError, setSizeError] = useState<string | null>(null);
  const [aware, setAware] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    try { setCart(JSON.parse(window.localStorage.getItem(CART_KEY) || "{}") || {}); } catch { /* sem armazenamento: carrinho só nesta aba */ }
    setCartReady(true);
  }, []);
  useEffect(() => {
    if (!cartReady) return;
    try { window.localStorage.setItem(CART_KEY, JSON.stringify(cart)); } catch { /* idem */ }
  }, [cart, cartReady]);
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(""), 4500);
    return () => window.clearTimeout(timer);
  }, [message]);

  const load = async (token: string | null) => {
    setLoading(true);
    try {
      const [catalog, history, me] = await Promise.all([api("/loja/produtos", token), api("/loja/meus-pedidos", token), api("/auth/me", token).catch(() => null)]);
      setAuth({ authenticated: true, user: { name: user?.fullName || user?.primaryEmailAddress?.emailAddress || "Colaborador" }, manager: me?.user?.role === "ADMINISTRADOR" });
      setProducts(catalog.items || []);
      setCart(current => sanitizeCart(current, catalog.items || []));
      setOrders(history.items || []);
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

  const groups = useMemo(() => groupProducts(products), [products]);
  const variants = useMemo(() => new Map(groups.flatMap(group => group.variants.map(variant => [variant.id, { variant, group }] as const))), [groups]);
  const lines = useMemo(() => Object.entries(cart).flatMap(([id, quantity]) => { const found = variants.get(Number(id)); return found && quantity > 0 ? [{ ...found, quantity }] : []; }), [cart, variants]);
  const count = lines.reduce((sum, line) => sum + line.quantity, 0);
  const totalCents = lines.reduce((sum, line) => sum + cents(line.variant.unit_price) * line.quantity, 0);
  const label = (group: Group, variant: Variant) => variant.size ? `${group.name} (${variant.size})` : group.name;

  const add = (group: Group) => {
    const variant = group.sized ? group.variants.find(candidate => candidate.id === selected[group.key]) : group.variants[0];
    if (!variant) { setSizeError(group.key); return; }
    const current = cart[variant.id] || 0;
    if (current >= variant.available_quantity) { setMessage(`Só há ${variant.available_quantity} unidade(s) de ${label(group, variant)} disponível(is).`); return; }
    setCart({ ...cart, [variant.id]: current + 1 });
    setMessage(`${label(group, variant)} adicionado ao pedido.`);
  };
  const changeQuantity = (id: number, delta: number) => {
    const found = variants.get(id);
    if (!found) return;
    const next = Math.min(found.variant.available_quantity, Math.max(1, (cart[id] || 0) + delta));
    setCart({ ...cart, [id]: next });
  };
  const remove = (id: number) => { const next = { ...cart }; delete next[id]; setCart(next); };

  const submit = async () => {
    if (!lines.length || submitting || !aware) return;
    setSubmitting(true); setMessage("");
    try {
      const token = await getToken();
      const result = await api("/loja/pedidos", token, { method: "POST", body: JSON.stringify({ items: lines.map(line => ({ product_id: line.variant.id, quantity: line.quantity })) }) });
      setCart({}); setAware(false);
      setMessage(`Pedido ${result.order_number} enviado! Agora ele passa pela aprovação do RH.`);
      await load(token);
      document.getElementById("meus-pedidos")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Não foi possível enviar o pedido.");
      void getToken().then(load); // atualiza o estoque mostrado, caso algo tenha acabado
    } finally { setSubmitting(false); }
  };

  const logout = async () => { await signOut(); window.location.reload(); };

  if (loading) return <main className="loja-page"><div className="loja-loading">Carregando a Lojinha...</div></main>;
  if (!isSignedIn || !auth?.authenticated) return <main className="loja-page"><section className="loja-login"><img className="loja-logo" src="/klubi-logo-black.png" alt="Klubi" /><p className="loja-eyebrow">LOJINHA KLUBI</p><h1>Acesse a Lojinha</h1><p>Entre com seu e-mail corporativo @klubi.com.br para fazer pedidos.</p><SignInButton mode="modal"><button className="loja-primary">Entrar com e-mail Klubi</button></SignInButton><a className="loja-back" href="/">Voltar</a></section></main>;

  return <main className="loja-page">
    <header className="loja-topbar">
      <div><img className="loja-header-logo" src="/klubi-logo-black.png" alt="Klubi" /><p className="loja-eyebrow">LOJINHA KLUBI</p><h1>Olá, {auth.user?.name || "colaborador"}</h1><p>Escolha os produtos e o tamanho, revise seu pedido e envie para aprovação.</p></div>
      <div className="loja-topbar-acoes">
        {auth.manager && <a className="loja-gestao" href="/">Painel de gestão</a>}
        <button className="loja-secondary" onClick={logout}>Sair</button>
      </div>
    </header>

    <section className="loja-layout">
      <div className="loja-catalog">
        <div className="loja-section-title"><h2>Produtos</h2><span>{groups.filter(group => group.variants.some(variant => variant.available_quantity > 0)).length} de {groups.length} disponíveis</span></div>
        {groups.length ? <div className="loja-grid">{groups.map(group => {
          const soldOut = group.variants.every(variant => variant.available_quantity <= 0);
          const chosen = group.sized ? group.variants.find(variant => variant.id === selected[group.key]) : group.variants[0];
          const prices = [...new Set(group.variants.map(variant => cents(variant.unit_price)))];
          const price = chosen ? money(chosen.unit_price) : prices.length > 1 ? `a partir de ${money(Math.min(...prices) / 100)}` : money(prices[0] / 100);
          return <article className={`loja-card ${soldOut ? "is-esgotado" : ""}`} key={group.key}>
            <div className="loja-card-foto">
              {group.image_url ? <img src={`${API}${group.image_url}`} alt={group.name} loading="lazy" /> : <span className="loja-card-sem-foto" aria-hidden="true">{group.name.charAt(0)}</span>}
              {soldOut && <span className="loja-selo">Esgotado</span>}
            </div>
            <div className="loja-card-info">
              <h3>{group.name}</h3>
              {group.description && <p className="loja-card-desc">{group.description}</p>}
              <strong className="loja-preco">{price}</strong>
              {group.sized && <fieldset className={`loja-tamanhos ${sizeError === group.key ? "tem-erro" : ""}`} disabled={soldOut}>
                <legend>Tamanho</legend>
                <div className="loja-chips">{group.variants.map(variant => <label className="loja-chip" key={variant.id} title={variant.available_quantity <= 0 ? "Esgotado" : undefined}>
                  <input type="radio" name={`tamanho-${group.key}`} value={variant.id} disabled={variant.available_quantity <= 0} checked={selected[group.key] === variant.id} onChange={() => { setSelected({ ...selected, [group.key]: variant.id }); setSizeError(null); }} />
                  <span>{variant.size || "Único"}</span>
                </label>)}</div>
                {sizeError === group.key && <p className="loja-erro" role="alert">Escolha um tamanho</p>}
              </fieldset>}
              {chosen && chosen.available_quantity > 0 && chosen.available_quantity <= 5 && <p className="loja-ultimas">Últimas {chosen.available_quantity} unidade(s)</p>}
              <button className="loja-primary" disabled={soldOut} onClick={() => add(group)}>{soldOut ? "Esgotado" : "Adicionar ao pedido"}</button>
            </div>
          </article>;
        })}</div> : <div className="loja-empty">Nenhum produto disponível no momento.</div>}
      </div>

      <aside className="loja-cart" id="carrinho">
        <div className="loja-section-title"><h2>Meu pedido</h2>{count > 0 && <span>{count} {count === 1 ? "item" : "itens"}</span>}</div>
        {lines.length ? <>
          <ul className="loja-cart-lines">{lines.map(({ variant, group, quantity }) => <li className="loja-cart-line" key={variant.id}>
            {group.image_url ? <img className="loja-cart-foto" src={`${API}${group.image_url}`} alt="" /> : <span className="loja-cart-foto loja-card-sem-foto" aria-hidden="true">{group.name.charAt(0)}</span>}
            <div className="loja-cart-info">
              <span className="loja-cart-nome">{group.name}</span>
              <span className="loja-muted">{variant.size ? `Tamanho ${variant.size} · ` : ""}{money(variant.unit_price)} cada</span>
              <div className="loja-qtd" role="group" aria-label={`Quantidade de ${label(group, variant)}`}>
                <button aria-label="Diminuir quantidade" disabled={quantity <= 1} onClick={() => changeQuantity(variant.id, -1)}>−</button>
                <span>{quantity}</span>
                <button aria-label="Aumentar quantidade" disabled={quantity >= variant.available_quantity} onClick={() => changeQuantity(variant.id, 1)}>+</button>
                <button className="loja-remover" onClick={() => remove(variant.id)}>Remover</button>
              </div>
            </div>
            <strong>{money(cents(variant.unit_price) * quantity / 100)}</strong>
          </li>)}</ul>
          <dl className="loja-resumo">
            <div><dt>Valor dos itens</dt><dd>{money(totalCents * 2 / 100)}</dd></div>
            <div className="loja-resumo-klubi"><dt>Klubi paga (50%)</dt><dd>− {money(totalCents / 100)}</dd></div>
            <div className="loja-total"><dt>Descontado em folha</dt><dd>{money(totalCents / 100)}</dd></div>
          </dl>
          <label className="loja-ciente"><input type="checkbox" checked={aware} onChange={event => setAware(event.target.checked)} /><span>Estou ciente de que <strong>{money(totalCents / 100)}</strong> será descontado da minha folha de pagamento.</span></label>
          <button className="loja-primary" disabled={submitting || !aware} onClick={submit}>{submitting ? "Enviando..." : "Enviar pedido"}</button>
          <small>Seu pedido passa pela aprovação do RH antes da entrega.</small>
        </> : <p className="loja-muted">Escolha os produtos para montar seu pedido.</p>}
      </aside>
    </section>

    <section className="loja-orders" id="meus-pedidos">
      <div className="loja-section-title"><h2>Meus pedidos</h2></div>
      {orders.length ? orders.map(order => <div className="loja-order" key={order.id}>
        <div className="loja-order-topo"><strong>{order.order_number}</strong><span className="loja-muted">{new Date(order.created_at).toLocaleString("pt-BR")}</span><span className={`loja-status ${String(order.status).toLowerCase()}`}>{statusLabel[order.status] || order.status}</span><strong className="loja-order-total">{money(order.total)}</strong></div>
        {Array.isArray(order.items) && order.items.length > 0 && <ul className="loja-order-itens">{order.items.map((item: AnyRecord) => <li key={item.id}>{item.quantity}× {item.product_name_snapshot}</li>)}</ul>}
      </div>) : <p className="loja-muted">Você ainda não fez pedidos.</p>}
    </section>

    {count > 0 && <a className="loja-barra-pedido" href="#carrinho"><span>Ver meu pedido · {count} {count === 1 ? "item" : "itens"}</span><strong>{money(totalCents / 100)}</strong></a>}
    {message && <div className="loja-message" role="status">{message}</div>}
  </main>;
}
