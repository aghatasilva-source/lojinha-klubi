"use client";

import { useAuth, useUser } from "@clerk/nextjs";
import { useCallback, useEffect, useMemo, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
// Sessão do Clerk de quem está logado. A API só libera o painel para e-mails em ADMIN_EMAILS.
let sessionToken: (() => Promise<string | null>) | null = null;
async function authHeaders(): Promise<Record<string, string>> {
  const token = sessionToken ? await sessionToken() : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}
type View = "inicio" | "pedidos" | "estoque" | "baixo" | "movimentacoes" | "fotos" | "concluidos" | "fechamento" | "relatorios" | "configuracoes";
type AnyRecord = Record<string, any>;
type StockForm = { product_id: number; quantity: number; reason: string; supplier: string };
type WithdrawalItem = { product_id: number; quantity: number };
type NonCommercialForm = { reason_type: string; recipient: string; observation: string; items: WithdrawalItem[] };

const money = (value: number) => `R$ ${(Number(value) || 0).toFixed(2).replace(".", ",")}`;
const movementLabel = (item: AnyRecord) => item.reference_type === "SAIDA_NAO_COMERCIAL" ? "Saída não comercial" : ({ ENTRADA: "Entrada", SAIDA: "Saída", RESERVA: "Reserva", LIBERACAO_RESERVA: "Liberação de reserva", BAIXA_PEDIDO: "Baixa de pedido", DEVOLUCAO: "Devolução", AJUSTE: "Ajuste", REPOSICAO: "Reposição" } as Record<string, string>)[item.movement_type] || item.movement_type;
const statusLabel: Record<string, string> = { PENDENTE: "Pendente", APROVADO: "Aprovado", CANCELADO: "Cancelado", CONCLUIDO: "Concluído" };

async function api(path: string, options?: RequestInit) {
  const response = await fetch(`${API}${path}`, { ...options, credentials: "include", headers: { "Content-Type": "application/json", ...(await authHeaders()), ...(options?.headers || {}) }, cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Não foi possível concluir a operação.");
  return data;
}

// Abaixo disso a foto fica borrada no cartão da loja em telas de alta densidade (cartão ~250 px × 2).
const MIN_SHARP_SIZE = 600;
const MAX_UPLOAD_SIZE = 1600;

// Reduz a foto no navegador antes de enviar (fotos de celular chegam com vários MB), sem nunca ampliar.
async function prepareImage(file: File): Promise<{ image: string; originalSize: number }> {
  if (!file.type.startsWith("image/")) throw new Error("Escolha um arquivo de imagem (JPG, PNG ou WebP).");
  let bitmap: ImageBitmap;
  try { bitmap = await createImageBitmap(file); } catch { throw new Error("Não foi possível abrir esta imagem. Tente salvar como JPG ou PNG."); }
  const originalSize = Math.max(bitmap.width, bitmap.height);
  const scale = Math.min(1, MAX_UPLOAD_SIZE / originalSize);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Seu navegador não conseguiu processar a imagem.");
  context.imageSmoothingEnabled = true;
  context.imageSmoothingQuality = "high";
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return { image: canvas.toDataURL("image/jpeg", 0.92), originalSize };
}

function registerUiLog(action: string, details: AnyRecord = {}) {
  void authHeaders().then(headers => fetch(`${API}/log`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify({ action, details }), keepalive: true })).catch(() => undefined);
}

export default function Home() {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const { user } = useUser();
  sessionToken = getToken;
  // "verificando" até a API confirmar o perfil; quem não é administrador vai para a loja.
  const [access, setAccess] = useState<"verificando" | "liberado">("verificando");
  useEffect(() => {
    if (!isLoaded) return;
    if (!isSignedIn) { window.location.replace("/loja"); return; }
    let active = true;
    api("/auth/me").then(me => {
      if (!active) return;
      if (me.user?.role === "ADMINISTRADOR") setAccess("liberado");
      else window.location.replace("/loja");
    }).catch(() => { if (active) window.location.replace("/loja"); });
    return () => { active = false; };
  }, [isLoaded, isSignedIn, user?.id]);
  return access === "liberado" ? <Painel userName={user?.firstName || user?.fullName || "Administrador"} /> : <div className="access-check">Verificando acesso...</div>;
}

function Painel({ userName }: { userName: string }) {
  const [view, setView] = useState<View>("inicio");
  const [hydrated, setHydrated] = useState(false);
  const [dashboard, setDashboard] = useState<AnyRecord>({});
  const [orders, setOrders] = useState<AnyRecord[]>([]);
  const [products, setProducts] = useState<AnyRecord[]>([]);
  const [movements, setMovements] = useState<AnyRecord[]>([]);
  const [completed, setCompleted] = useState<AnyRecord[]>([]);
  const [reports, setReports] = useState<AnyRecord>({});
  const [productImages, setProductImages] = useState<AnyRecord[]>([]);
  const [uploadingImage, setUploadingImage] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [orderFilter, setOrderFilter] = useState("TODOS");
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState("");
  const [undoId, setUndoId] = useState<number | null>(null);
  const [processing, setProcessing] = useState<{ id: number; action: string } | null>(null);
  const [stockImporting, setStockImporting] = useState(false);
  const [withdrawalOpen, setWithdrawalOpen] = useState(false);
  const [withdrawalSubmitting, setWithdrawalSubmitting] = useState(false);
  const [withdrawalForm, setWithdrawalForm] = useState<NonCommercialForm>({ reason_type: "KIT_INTEGRACAO", recipient: "", observation: "", items: [{ product_id: 0, quantity: 1 }] });
  const [confirm, setConfirm] = useState<{ id: number; action: "aprovar" | "cancelar" | "concluir" } | null>(null);
  const [stockForm, setStockForm] = useState<StockForm>({ product_id: 0, quantity: 1, reason: "", supplier: "" });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [dash, orderData, productData, movementData, completedData, reportData, imageData] = await Promise.all([
        api("/dashboard"), api(`/pedidos?status=${orderFilter}&busca=${encodeURIComponent(search)}`), api(`/produtos?baixo=${view === "baixo"}`), api("/movimentacoes"), api("/pedidos-concluidos"), api("/relatorios"), api("/produtos/imagens"),
      ]);
      setDashboard(dash); setOrders(orderData.items); setProducts(productData.items); setMovements(movementData.items); setCompleted(completedData.items); setReports(reportData); setProductImages(imageData.items);
    } catch (error) { showToast(error instanceof Error ? error.message : "Falha ao carregar os dados."); } finally { setLoading(false); }
  }, [orderFilter, search, view]);

  useEffect(() => {
      const savedView = window.localStorage.getItem("lojinha-klubi-view") as View | null;
      if (savedView) setView(savedView);
    const savedUndoId = window.localStorage.getItem("lojinha-klubi-undo-order-v2");
    if (savedUndoId) setUndoId(Number(savedUndoId));
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (hydrated) window.localStorage.setItem("lojinha-klubi-view", view);
  }, [hydrated, view]);

  useEffect(() => {
    if (!hydrated) return;
    if (undoId === null) window.localStorage.removeItem("lojinha-klubi-undo-order-v2");
    else window.localStorage.setItem("lojinha-klubi-undo-order-v2", String(undoId));
  }, [hydrated, undoId]);

  useEffect(() => {
    const handleClick = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      const clickable = target?.closest("button, a, [role='button'], .submenu div");
      if (clickable) registerUiLog("CLIQUE_INTERFACE", { label: clickable.textContent?.trim(), aria_label: clickable.getAttribute("aria-label") || undefined, element: clickable.tagName.toLowerCase() });
    };
    const handleChange = (event: Event) => {
      const target = event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null;
      if (target) registerUiLog("EDICAO_CAMPO", { name: target.name || target.getAttribute("aria-label") || target.getAttribute("placeholder") || target.type || target.tagName });
    };
    document.addEventListener("click", handleClick);
    document.addEventListener("change", handleChange);
    return () => { document.removeEventListener("click", handleClick); document.removeEventListener("change", handleChange); };
  }, []);

  useEffect(() => { load(); }, [load]);
  const showToast = (message: string, canUndo = false) => { setToast(message); if (!canUndo) setUndoId(null); window.setTimeout(() => setToast(""), 5000); };
  const runOrderAction = async () => {
    if (!confirm) return;
    const selected = confirm;
    setProcessing({ id: selected.id, action: selected.action });
    try { await api(`/pedidos/${selected.id}/${selected.action}`, { method: "POST", headers: { "x-idempotency-key": `${selected.action}-${selected.id}` } }); setUndoId(selected.id); setConfirm(null); showToast(selected.action === "aprovar" ? "Pedido aprovado, estoque baixado e enviado para concluídos." : selected.action === "concluir" ? "Pedido concluído e histórico atualizado." : "Pedido cancelado.", true); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível executar a ação."); } finally { setProcessing(null); }
  };
  const undoOrder = async (targetId?: number) => { const orderId = targetId ?? undoId; if (orderId === null || orderId === undefined) return; setProcessing({ id: orderId, action: "desfazer" }); try { await api(`/pedidos/${orderId}/desfazer`, { method: "POST", headers: { "x-idempotency-key": `desfazer-${orderId}-${Date.now()}` } }); registerUiLog("DESFAZER_PEDIDO", { order_id: orderId }); setUndoId(null); showToast("Ação desfeita com sucesso."); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível desfazer a ação."); } finally { setProcessing(null); } };
  const sync = async () => { try { const result = await api("/sincronizar", { method: "POST" }); showToast(`${result.importados || 0} novo(s) pedido(s) sincronizado(s).`); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Falha na sincronização."); } };
  const closeMonth = async () => { const competence = new Date().toISOString().slice(0, 7); if (!window.confirm("Deseja fechar esta competência? Após o fechamento, os valores ficarão registrados no histórico e não poderão ser alterados sem ação administrativa.")) return; try { await api(`/fechamento-mensal/${competence}/fechar`, { method: "POST" }); showToast("Fechamento mensal realizado com sucesso."); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível fechar a competência."); } };
  const exportMonth = async () => { const competence = new Date().toISOString().slice(0, 7); try { const response = await fetch(`${API}/fechamento-mensal/${competence}/csv`, { headers: await authHeaders() }); if (!response.ok) throw new Error("Não foi possível exportar o fechamento."); const blob = await response.blob(); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `fechamento-${competence}.csv`; link.click(); URL.revokeObjectURL(link.href); } catch (error) { showToast(error instanceof Error ? error.message : "Falha na exportação."); } };
  const addStock = async (event: React.FormEvent) => { event.preventDefault(); try { await api("/estoque/entrada", { method: "POST", body: JSON.stringify(stockForm) }); setStockForm({ product_id: 0, quantity: 1, reason: "", supplier: "" }); showToast("Estoque atualizado com sucesso."); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível atualizar o estoque."); } };
  const importStock = async () => { if (stockImporting) return; setStockImporting(true); try { const preview = await api("/estoque/importar"); if (preview.nao_identificados?.length) { const itens = preview.nao_identificados.slice(0, 3).map((item: AnyRecord) => `${item.produto} ${item.tamanho}`).join(", "); showToast(`${preview.nao_identificados.length} item(ns) ambíguo(s): ${itens}${preview.nao_identificados.length > 3 ? "..." : ""}. Importação bloqueada.`); return; } if (!preview.total_alteracoes) { showToast(preview.nao_cadastrados?.length ? `Nenhum saldo mudou. ${preview.nao_cadastrados.length} item(ns) da planilha ainda não está cadastrado.` : "O estoque já está igual à planilha."); return; } const ignorados = preview.nao_cadastrados?.length ? ` ${preview.nao_cadastrados.length} item(ns) não cadastrado(s) serão ignorados.` : ""; const confirmed = window.confirm(`A planilha será aplicada sobre o saldo atual de ${preview.total_alteracoes} produto(s), usando Eugênio e Grand Station.${ignorados} Deseja continuar?`); if (!confirmed) return; const result = await api("/estoque/importar", { method: "POST", body: JSON.stringify({ confirm: true }) }); showToast(`${result.atualizados || 0} produto(s) atualizado(s) pelo inventário físico.${ignorados}`); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível importar o estoque."); } finally { setStockImporting(false); } };

  const submitNonCommercial = async (event: React.FormEvent) => { event.preventDefault(); if (withdrawalSubmitting) return; setWithdrawalSubmitting(true); try { const selectedItems = withdrawalForm.items.filter(item => item.product_id > 0 && item.quantity > 0); if (!selectedItems.length) throw new Error("Selecione pelo menos um produto."); await api("/estoque/saida-nao-comercial", { method: "POST", body: JSON.stringify({ ...withdrawalForm, items: selectedItems }) }); registerUiLog("SAIDA_NAO_COMERCIAL", { reason_type: withdrawalForm.reason_type, recipient: withdrawalForm.recipient, item_count: selectedItems.length }); setWithdrawalForm({ reason_type: "KIT_INTEGRACAO", recipient: "", observation: "", items: [{ product_id: 0, quantity: 1 }] }); setWithdrawalOpen(false); showToast("Saída não comercial registrada e estoque atualizado."); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível registrar a saída."); } finally { setWithdrawalSubmitting(false); } };
  const uploadImage = async (baseName: string, file: File) => { if (uploadingImage) return; setUploadingImage(baseName); try { const { image, originalSize } = await prepareImage(file); await api("/produtos/imagem", { method: "POST", body: JSON.stringify({ base_name: baseName, image }) }); showToast(originalSize < MIN_SHARP_SIZE ? `Foto de ${baseName} enviada, mas é pequena (${originalSize} px) e pode ficar borrada na loja. Prefira 800 px ou mais.` : `Foto de ${baseName} atualizada. Ela já aparece na loja.`); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível enviar a foto."); } finally { setUploadingImage(null); } };
  const removeImage = async (baseName: string) => { if (uploadingImage || !window.confirm(`Remover a foto de ${baseName}? Na loja, o produto aparecerá sem imagem.`)) return; setUploadingImage(baseName); try { await api("/produtos/imagem/remover", { method: "POST", body: JSON.stringify({ base_name: baseName }) }); showToast(`Foto de ${baseName} removida.`); await load(); } catch (error) { showToast(error instanceof Error ? error.message : "Não foi possível remover a foto."); } finally { setUploadingImage(null); } };
  const pageTitle: Record<View, string> = { inicio: "Início", pedidos: "Pedidos", estoque: "Estoque", baixo: "Estoque baixo", movimentacoes: "Histórico de movimentações", fotos: "Fotos da loja", concluidos: "Pedidos concluídos", fechamento: "Fechamento mensal", relatorios: "Relatórios", configuracoes: "Configurações" };
  const pendingOrders = useMemo(() => orders.filter(order => order.status === "PENDENTE"), [orders]);

  return <div className="app">
    <aside className="sidebar"><div className="brand"><div className="logo"><img src="/klubi-logo-white.png" alt="Klubi" /></div><div className="brand-copy"><strong>GESTÃO INTERNA</strong><small>Lojinha Klubi</small></div></div><div className="menu-title">Operação</div>
      <Nav icon="⌂" label="Início" active={view === "inicio"} onClick={() => setView("inicio")} />
      <Nav icon="▣" label="Pedidos" active={view === "pedidos"} onClick={() => setView("pedidos")} />
      <a className="menu-button" href="/loja"><span className="menu-icon">🛍</span><span>Lojinha</span></a>
      <Nav icon="▤" label="Estoque" active={view === "estoque" || view === "baixo" || view === "movimentacoes" || view === "fotos"} onClick={() => setView("estoque")} />
      {(view === "estoque" || view === "baixo" || view === "movimentacoes" || view === "fotos") && <div className="submenu"><div className={view === "estoque" ? "active" : ""} onClick={() => setView("estoque")}>Estoque atual</div><div className={view === "movimentacoes" ? "active" : ""} onClick={() => setView("movimentacoes")}>Histórico de movimentações</div><div className={view === "baixo" ? "active" : ""} onClick={() => setView("baixo")}>Estoque baixo</div><div className={view === "fotos" ? "active" : ""} onClick={() => setView("fotos")}>Fotos da loja</div></div>}
      <Nav icon="✓" label="Pedidos concluídos" active={view === "concluidos"} onClick={() => setView("concluidos")} /><Nav icon="▣" label="Fechamento mensal" active={view === "fechamento"} onClick={() => setView("fechamento")} /><Nav icon="▥" label="Relatórios" active={view === "relatorios"} onClick={() => setView("relatorios")} /><Nav icon="⚙" label="Configurações" active={view === "configuracoes"} onClick={() => setView("configuracoes")} />
    </aside>
    <main className="main"><header className="topbar"><input className="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Buscar na Lojinha Klubi..." /><div className="top-actions"><button className="bell" aria-label="Notificações">♟<span>{dashboard.produtos_estoque_baixo || 0}</span></button><div className="user"><div className="avatar">{userName.charAt(0).toUpperCase()}</div><span>{userName}</span></div></div></header>
      <div className="content"><div className="page-head"><div><h1>{view === "inicio" ? `Olá, ${userName} 👋` : pageTitle[view]}</h1><p>{view === "inicio" ? "Confira o resumo da Lojinha Klubi hoje." : "Acompanhe a operação com segurança e rastreabilidade."}</p></div><div className="page-head-actions">{view === "inicio" && <button className="secondary" onClick={sync}>↻ Sincronizar pedidos</button>}{view === "estoque" && <><button className="secondary" onClick={importStock} disabled={stockImporting}>{stockImporting ? "Importando..." : "↥ Importar estoque atual"}</button><button className="secondary" onClick={() => setWithdrawalOpen(true)}>− Saída não comercial</button><button className="primary" onClick={() => document.getElementById("entrada")?.scrollIntoView({ behavior: "smooth" })}>+ Adicionar ao estoque</button></>}</div></div>
        {loading && <div className="muted" style={{ marginBottom: 14 }}>Carregando...</div>}
        {view === "inicio" && <Dashboard dashboard={dashboard} orders={orders} onLow={() => setView("baixo")} onOrders={() => setView("pedidos")} />}
        {view === "pedidos" && <Orders orders={orders} filter={orderFilter} setFilter={setOrderFilter} onAction={setConfirm} onUndo={undoOrder} undoId={undoId} processing={processing} />}
        {(view === "estoque" || view === "baixo") && <Stock products={products} low={view === "baixo"} form={stockForm} setForm={setStockForm} onSubmit={addStock} />}
        {view === "movimentacoes" && <Movements data={movements} />}
        {view === "fotos" && <Photos items={productImages} uploading={uploadingImage} onUpload={uploadImage} onRemove={removeImage} />}
        {view === "concluidos" && <Completed data={completed} />}
        {view === "fechamento" && <Closing onClose={closeMonth} onExport={exportMonth} />}
        {view === "relatorios" && <Reports data={reports} />}
        {view === "configuracoes" && <Settings />}
      </div></main>
    {confirm && <div className="modal-backdrop"><div className="modal"><h2>{confirm.action === "aprovar" ? "Aprovar pedido" : confirm.action === "concluir" ? "Concluir pedido" : "Cancelar pedido"}</h2><p>{confirm.action === "aprovar" ? "Deseja aprovar este pedido? Ao confirmar, os produtos serão baixados do estoque." : confirm.action === "concluir" ? "Confirma que este pedido foi entregue? O histórico de pedidos será atualizado." : "Deseja cancelar este pedido? Nenhuma unidade será retirada do estoque."}</p><div className="modal-actions"><button className="secondary" disabled={processing !== null} onClick={() => setConfirm(null)}>Voltar</button><button className={`primary ${confirm.action === "cancelar" ? "danger" : ""}`} disabled={processing?.id === confirm.id} onClick={runOrderAction}>{processing?.id === confirm.id ? <span className="button-spinner" aria-label="Processando" /> : confirm.action === "aprovar" ? "Sim, aprovar pedido" : confirm.action === "concluir" ? "Sim, concluir pedido" : "Cancelar pedido"}</button></div></div></div>}
    {withdrawalOpen && <NonCommercialModal products={products} form={withdrawalForm} setForm={setWithdrawalForm} submitting={withdrawalSubmitting} onClose={() => setWithdrawalOpen(false)} onSubmit={submitNonCommercial} />}
    {toast && <div className="toast"><span>{toast}</span></div>}
  </div>;
}

function Nav({ icon, label, active, onClick }: { icon: string; label: string; active: boolean; onClick: () => void }) { return <button className={`menu-button ${active ? "active" : ""}`} onClick={onClick}><span className="menu-icon">{icon}</span><span>{label}</span></button>; }
function Dashboard({ dashboard, orders, onLow, onOrders }: { dashboard: AnyRecord; orders: AnyRecord[]; onLow: () => void; onOrders: () => void }) { return <><div className="grid cards"><Metric label="Pedidos pendentes" value={dashboard.pedidos_pendentes || 0} icon="◷" /><Metric label="Pedidos aprovados hoje" value={dashboard.pedidos_aprovados_hoje || 0} icon="✓" green /><Metric label="Pedidos concluídos" value={dashboard.pedidos_concluidos || 0} icon="▣" /><Metric label="Produtos com estoque baixo" value={dashboard.produtos_estoque_baixo || 0} icon="!" red /></div>{dashboard.produtos_estoque_baixo > 0 && <div className="alert"><span>Atenção: existem produtos que precisam de reposição.</span><button onClick={onLow}>Ver estoque baixo →</button></div>}<div className="grid section-grid"><section className="card"><div className="section-title"><h2>Pedidos recentes</h2><button className="secondary" onClick={onOrders}>Ver todos</button></div><MiniOrders orders={orders.slice(0, 5)} /></section><section className="card"><div className="section-title"><h2>Resumo do estoque</h2></div><div style={{ fontSize: 34, fontWeight: 800 }}>{dashboard.total_itens_disponiveis || 0}</div><div className="muted">itens disponíveis no momento</div><div style={{ marginTop: 23 }}><div className="muted">Situação da operação</div><div style={{ marginTop: 8, color: "#28733b", fontWeight: 700 }}>● Monitoramento ativo</div></div></section></div></>; }
function Metric({ label, value, icon, red, green }: { label: string; value: number; icon: string; red?: boolean; green?: boolean }) { return <div className={`card metric ${red ? "red" : ""} ${green ? "green" : ""}`}><div><div className="metric-label">{label}</div><div className="metric-value">{value}</div></div><div className="metric-icon">{icon}</div></div>; }
function MiniOrders({ orders }: { orders: AnyRecord[] }) { return orders.length ? <div>{orders.map(order => <div key={order.id} style={{ display: "flex", justifyContent: "space-between", padding: "12px 0", borderBottom: "1px solid #f0f1f3", gap: 12 }}><div><strong style={{ fontSize: 13 }}>{order.order_number}</strong><div className="muted" style={{ marginTop: 4 }}>{order.customer_name}</div></div><span className={`tag ${order.status.toLowerCase()}`}>{statusLabel[order.status]}</span></div>)}</div> : <div className="empty"><strong>Nenhum pedido pendente 🎉</strong>Tudo certo por aqui.</div>; }
function Orders({ orders, filter, setFilter, onAction, onUndo, undoId, processing }: { orders: AnyRecord[]; filter: string; setFilter: (value: string) => void; onAction: (action: { id: number; action: "aprovar" | "cancelar" | "concluir" }) => void; onUndo: (orderId: number) => void; undoId: number | null; processing: { id: number; action: string } | null }) { return <><div className="filters">{[["TODOS", "Todos"], ["PENDENTE", "Pendentes"], ["APROVADO", "Aprovados"], ["CANCELADO", "Cancelados"], ["CONCLUIDO", "Concluídos"]].map(([value, label]) => <button key={value} className={`filter ${filter === value ? "active" : ""}`} onClick={() => setFilter(value)}>{label}</button>)}</div><div className="table-wrap"><table className="table"><thead><tr><th>Número do pedido</th><th>Colaborador</th><th>E-mail</th><th>Carimbo/datahora</th><th>Produto</th><th>Quantidade</th><th>Tamanho</th><th>Valor</th><th>Status</th><th>Ações</th></tr></thead><tbody>{orders.length ? orders.map(order => <tr key={order.id}><td><strong>{order.order_number}</strong></td><td>{order.customer_name}</td><td>{order.customer_email || order.customer_identifier}</td><td>{new Date(order.source_created_at || order.created_at).toLocaleString("pt-BR")}</td><td>{order.products || "—"}</td><td>{order.quantities || "—"}</td><td>{order.sizes || "—"}</td><td>{money(order.total)}</td><td><span className={`tag ${order.status.toLowerCase()}`}>{statusLabel[order.status]}</span></td><td>{processing?.id === order.id ? <button className="undo-button" disabled><span className="button-spinner" aria-label="Processando" /></button> : order.status === "PENDENTE" && <><button className="secondary" onClick={() => onAction({ id: order.id, action: "aprovar" })}>Aprovar</button> <button className="secondary danger" onClick={() => onAction({ id: order.id, action: "cancelar" })}>Cancelar</button></>}{processing?.id !== order.id && ["APROVADO", "CANCELADO", "CONCLUIDO"].includes(order.status) && <button className="undo-button" onClick={() => onUndo(order.id)}>Desfazer</button>}</td></tr>) : <tr><td colSpan={10}><div className="empty"><strong>Nenhum pedido encontrado</strong>Experimente limpar os filtros.</div></td></tr>}</tbody></table></div></>; }
function NonCommercialModal({ products, form, setForm, submitting, onClose, onSubmit }: { products: AnyRecord[]; form: NonCommercialForm; setForm: React.Dispatch<React.SetStateAction<NonCommercialForm>>; submitting: boolean; onClose: () => void; onSubmit: (event: React.FormEvent) => void }) { const updateItem = (index: number, patch: Partial<WithdrawalItem>) => setForm(current => ({ ...current, items: current.items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item) })); const addItem = () => setForm(current => ({ ...current, items: [...current.items, { product_id: 0, quantity: 1 }] })); const removeItem = (index: number) => setForm(current => ({ ...current, items: current.items.length === 1 ? current.items : current.items.filter((_, itemIndex) => itemIndex !== index) })); return <div className="modal-backdrop"><div className="modal modal-wide"><h2>Registrar saída não comercial</h2><p>Use esta operação para kits de integração, brindes ou retiradas internas. Ela dará baixa no estoque, mas não criará um pedido da lojinha.</p><form className="form" onSubmit={onSubmit}><label>Motivo<select value={form.reason_type} onChange={event => setForm({ ...form, reason_type: event.target.value })}><option value="KIT_INTEGRACAO">Kit de integração</option><option value="BRINDE">Brinde</option><option value="USO_INTERNO">Uso interno</option><option value="PERDA_AVARIA">Perda ou avaria</option><option value="OUTRO">Outro</option></select></label><label>Colaborador ou destinatário<input required value={form.recipient} onChange={event => setForm({ ...form, recipient: event.target.value })} placeholder="Ex.: João da Silva" /></label><label>Observação<textarea rows={2} value={form.observation} onChange={event => setForm({ ...form, observation: event.target.value })} placeholder="Opcional" /></label><div className="withdrawal-items"><div className="section-title"><h3>Produtos</h3><button className="secondary" type="button" onClick={addItem}>+ Adicionar produto</button></div>{form.items.map((item, index) => <div className="withdrawal-row" key={index}><select required value={item.product_id} onChange={event => updateItem(index, { product_id: Number(event.target.value) })}><option value={0}>Selecione um produto</option>{products.filter(product => product.available_quantity > 0 || product.id === item.product_id).map(product => <option key={product.id} value={product.id}>{product.name} — disponível: {product.available_quantity}</option>)}</select><input aria-label="Quantidade" type="number" min={1} required value={item.quantity} onChange={event => updateItem(index, { quantity: Number(event.target.value) })} /><button className="secondary" type="button" onClick={() => removeItem(index)} disabled={form.items.length === 1}>Remover</button></div>)}</div><div className="modal-actions"><button className="secondary" type="button" disabled={submitting} onClick={onClose}>Cancelar</button><button className="primary" type="submit" disabled={submitting}>{submitting ? <span className="button-spinner" aria-label="Processando" /> : "Registrar saída"}</button></div></form></div></div>; }

function Stock({ products, low, form, setForm, onSubmit }: { products: AnyRecord[]; low: boolean; form: StockForm; setForm: React.Dispatch<React.SetStateAction<StockForm>>; onSubmit: (event: React.FormEvent) => void }) { return <><div className="grid cards"><Metric label="Produtos cadastrados" value={products.length} icon="▤" /><Metric label="Total de itens disponíveis" value={products.reduce((sum, item) => sum + (item.available_quantity || 0), 0)} icon="#" green /><Metric label="Produtos com estoque baixo" value={products.filter(item => item.available_quantity <= 20).length} icon="!" red /><Metric label="Produtos sem estoque" value={products.filter(item => item.available_quantity === 0).length} icon="×" /></div><div className="table-wrap" style={{ marginTop: 22 }}><table className="table"><thead><tr><th>Foto</th><th>Produto</th><th>Código/SKU</th><th>Categoria</th><th>Físico</th><th>Reservado</th><th>Disponível</th><th>Situação</th></tr></thead><tbody>{products.length ? products.map(item => <tr key={item.id}><td><Thumb url={item.image_url} name={item.group_name || item.base_name} /></td><td><strong>{item.name}</strong></td><td>{item.sku}</td><td>{item.category}</td><td>{item.physical_quantity}</td><td>{item.reserved_quantity}</td><td>{item.available_quantity}</td><td><span className={`tag ${item.available_quantity <= 20 ? "low" : "stock"}`}>{item.situation}</span></td></tr>) : <tr><td colSpan={8}><div className="empty"><strong>Tudo certo por aqui.</strong>Nenhum produto está com estoque baixo.</div></td></tr>}</tbody></table></div>{!low && <section className="card" id="entrada" style={{ marginTop: 22 }}><div className="section-title"><h2>+ Adicionar ao estoque</h2></div><form className="form" onSubmit={onSubmit}><label>Produto<select value={form.product_id} onChange={e => setForm({ ...form, product_id: Number(e.target.value) })}><option value={0}>Selecione um produto</option>{products.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Quantidade<input type="number" min={1} value={form.quantity} onChange={e => setForm({ ...form, quantity: Number(e.target.value) })} /></label><label>Fornecedor<input value={form.supplier} onChange={e => setForm({ ...form, supplier: e.target.value })} placeholder="Opcional" /></label><label>Observação / justificativa<input required value={form.reason} onChange={e => setForm({ ...form, reason: e.target.value })} placeholder="Informe o motivo da entrada" /></label><button className="primary" type="submit">Adicionar ao estoque</button></form></section>}</>; }
function Thumb({ url, name }: { url?: string | null; name: string }) { return url ? <img className="thumb" src={`${API}${url}`} alt={name} loading="lazy" /> : <span className="thumb thumb-empty" title="Sem foto">—</span>; }
function PhotoFrame({ url, name }: { url?: string | null; name: string }) {
  const [size, setSize] = useState<number | null>(null);
  if (!url) return <div className="photo-frame"><span>Sem foto</span></div>;
  const lowRes = size !== null && size < MIN_SHARP_SIZE;
  return <div className="photo-frame">
    <img src={`${API}${url}`} alt={name} loading="lazy" onLoad={event => setSize(Math.max(event.currentTarget.naturalWidth, event.currentTarget.naturalHeight))} />
    {lowRes && <span className="photo-lowres" title={`A foto tem ${size} px. Envie uma de pelo menos 800 px para ficar nítida na loja.`}>Baixa resolução · {size} px</span>}
  </div>;
}
function Photos({ items, uploading, onUpload, onRemove }: { items: AnyRecord[]; uploading: string | null; onUpload: (baseName: string, file: File) => void; onRemove: (baseName: string) => void }) {
  const missing = items.filter(item => !item.image_url).length;
  return <>
    <p className="muted photos-hint">{missing ? <strong>{missing} produto(s) ainda sem foto. </strong> : "Todos os produtos têm foto. "}A foto vale para todos os tamanhos do produto e aparece na loja dos colaboradores assim que é enviada. Para ficar nítida, use a foto original com pelo menos 800 px, de preferência quadrada e com fundo branco (prints e recortes costumam ficar borrados).</p>
    {items.length ? <div className="photo-grid">{items.map(item => { const busy = uploading === item.base_name; return <article className="card photo-card" key={item.base_name}>
      <PhotoFrame url={item.image_url} name={item.base_name} />
      <div><strong>{item.base_name}</strong><div className="muted">{item.variations} {item.variations === 1 ? "variação" : "variações"}</div></div>
      <div className="photo-actions">
        <label className={`primary photo-upload ${busy || uploading ? "is-busy" : ""}`}>{busy ? <span className="button-spinner" aria-label="Enviando" /> : item.image_url ? "Trocar foto" : "Enviar foto"}<input type="file" accept="image/jpeg,image/png,image/webp" aria-label={`Foto de ${item.base_name}`} disabled={uploading !== null} onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) onUpload(item.base_name, file); }} /></label>
        {item.image_url && <button className="secondary" disabled={uploading !== null} onClick={() => onRemove(item.base_name)}>Remover</button>}
      </div>
    </article>; })}</div> : <div className="card empty"><strong>Nenhum produto cadastrado</strong>Importe o estoque para cadastrar os produtos.</div>}
  </>;
}
function Movements({ data }: { data: AnyRecord[] }) { return <div className="table-wrap"><table className="table"><thead><tr><th>Data e horário</th><th>Produto</th><th>Tipo</th><th>Quantidade</th><th>Pedido relacionado</th><th>Responsável</th><th>Saldo após</th><th>Observação</th></tr></thead><tbody>{data.map(item => <tr key={item.id}><td>{new Date(item.created_at).toLocaleString("pt-BR")}</td><td>{item.product_name}</td><td>{movementLabel(item)}</td><td>{item.quantity}</td><td>{item.order_number || "—"}</td><td>{item.responsible || "Administrador local"}</td><td>{item.quantity_after}</td><td>{item.reason}</td></tr>)}</tbody></table></div>; }
function Completed({ data }: { data: AnyRecord[] }) { return <div className="table-wrap"><table className="table"><thead><tr><th>Número do pedido</th><th>Colaborador</th><th>E-mail</th><th>Carimbo/datahora</th><th>Produto</th><th>Quantidade</th><th>Tamanho</th><th>Valor</th><th>Situação</th></tr></thead><tbody>{data.length ? data.map(order => <tr key={order.id}><td><strong>{order.order_number}</strong></td><td>{order.customer_name}</td><td>{order.customer_email || order.customer_identifier}</td><td>{new Date(order.source_created_at || order.confirmed_at || order.created_at).toLocaleString("pt-BR")}</td><td>{order.products || "—"}</td><td>{order.quantities || "—"}</td><td>{order.sizes || "—"}</td><td>{money(order.total)}</td><td><span className={`tag ${order.status.toLowerCase()}`}>{order.situation}</span></td></tr>) : <tr><td colSpan={9}><div className="empty"><strong>Nenhum pedido aprovado ou concluído</strong>Os pedidos aparecerão aqui após a aprovação.</div></td></tr>}</tbody></table></div>; }
function Closing({ onClose, onExport }: { onClose: () => void; onExport: () => void }) { return <div className="card"><div className="section-title"><h2>Fechamento mensal</h2><div className="page-head-actions"><button className="secondary" onClick={onExport}>Exportar CSV</button><button className="primary" onClick={onClose}>Fechar mês</button></div></div><p className="muted">O fechamento ocorre no dia 30 de cada mês. Após o fechamento, os valores ficam registrados no histórico e exigem ação administrativa para alteração.</p><div className="empty"><strong>Nenhum fechamento registrado</strong>Os pedidos concluídos aparecerão aqui por competência.</div></div>; }
function Reports({ data }: { data: AnyRecord }) { return <div className="grid section-grid"><section className="card"><div className="section-title"><h2>Pedidos por mês</h2></div>{(data.por_mes || []).map((row: AnyRecord) => <div key={row.competence} style={{ display: "flex", justifyContent: "space-between", padding: "12px 0", borderBottom: "1px solid #f0f1f3" }}><span>{row.competence}</span><strong>{row.orders_count} pedidos · {money(row.total)}</strong></div>)}</section><section className="card"><div className="section-title"><h2>Produtos mais pedidos</h2></div>{(data.produtos_mais_pedidos || []).map((row: AnyRecord) => <div key={row.name} style={{ display: "flex", justifyContent: "space-between", padding: "12px 0", borderBottom: "1px solid #f0f1f3" }}><span>{row.name}</span><strong>{row.quantity} un.</strong></div>)}</section></div>; }
function Settings() { return <div className="grid section-grid"><section className="card"><div className="section-title"><h2>Regras da operação</h2></div><div className="form"><label>Limite de estoque baixo<input defaultValue={20} type="number" /></label><label>Dia do fechamento mensal<input defaultValue={30} type="number" /></label><button className="primary">Salvar alterações</button></div></section><section className="card"><div className="section-title"><h2>Perfis de acesso</h2></div><p><strong>Administrador</strong><br /><span className="muted">Acesso completo.</span></p><p><strong>Operação</strong><br /><span className="muted">Pedidos, estoque, histórico e relatórios.</span></p><p><strong>Consulta</strong><br /><span className="muted">Somente visualização.</span></p></section></div>; }
