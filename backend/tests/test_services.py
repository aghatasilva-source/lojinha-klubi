from __future__ import annotations

import base64
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from backend.app import db, services
from backend.app.config import settings as default_settings
from backend.app.errors import InsufficientStockError, InvalidOrderStatusError, ProductNotFoundError


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.db"
        test_settings = replace(default_settings, root=Path(self.temp.name), database_path=self.db_path, pedidos_concluidos_xlsx=Path(self.temp.name) / "concluidos.xlsx", images_dir=Path(self.temp.name) / "imagens")
        db.settings = test_settings
        services.settings = test_settings
        db.initialize()
        with db.transaction(immediate=True) as connection:
            now = db.utc_now()
            product_id = connection.execute("INSERT INTO products(sku,name,base_name,size,source_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("TEST-1", "Camiseta Branca - P", "Camiseta Branca", "P", "test:1", now, now)).lastrowid
            connection.execute("INSERT INTO inventory(product_id,physical_quantity,updated_at) VALUES (?,?,?)", (product_id, 1, now))
            order_id = connection.execute("INSERT INTO orders(order_number,customer_identifier,customer_name,status,source_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("KLUBI-TEST-1", "a@klubi.com", "A", "PENDENTE", "order:1", now, now)).lastrowid
            connection.execute("INSERT INTO order_items(order_id,product_id,sku_snapshot,product_name_snapshot,quantity,unit_price_cents,total_price_cents,created_at) VALUES (?,?,?,?,?,?,?,?)", (order_id, product_id, "TEST-1", "Camiseta Branca - P", 1, 1700, 1700, now))
            connection.execute("UPDATE orders SET subtotal_cents=1700,total_cents=1700 WHERE id=?", (order_id,))
        self.product_id = product_id
        self.order_id = order_id

    def tearDown(self):
        self.temp.cleanup()

    def test_estoque_disponivel_e_aprovacao_sao_atomicos(self):
        result = services.approve_order(self.order_id, idempotency_key="approve-1")
        self.assertEqual(result["status"], "APROVADO")
        with db.connect() as connection:
            inventory = connection.execute("SELECT physical_quantity,reserved_quantity,available_quantity FROM inventory WHERE product_id=?", (self.product_id,)).fetchone()
            movements = connection.execute("SELECT movement_type FROM stock_movements WHERE product_id=?", (self.product_id,)).fetchall()
        self.assertEqual(tuple(inventory), (0, 0, 0))
        self.assertEqual([row[0] for row in movements], ["RESERVA", "BAIXA_PEDIDO"])

    def test_idempotencia_nao_baixa_duas_vezes(self):
        first = services.approve_order(self.order_id, idempotency_key="same-key")
        second = services.approve_order(self.order_id, idempotency_key="same-key")
        self.assertEqual(first["id"], second["id"])
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 2)

    def test_estoque_insuficiente_faz_rollback(self):
        with db.transaction(immediate=True) as connection:
            connection.execute("UPDATE order_items SET quantity=2,total_price_cents=3400 WHERE order_id=?", (self.order_id,))
        with self.assertRaises(InsufficientStockError):
            services.approve_order(self.order_id)
        with db.connect() as connection:
            inventory = connection.execute("SELECT physical_quantity,reserved_quantity FROM inventory WHERE product_id=?", (self.product_id,)).fetchone()
            self.assertEqual(tuple(inventory), (1, 0))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 0)

    def test_cancelamento_pendente_nao_mexe_no_estoque(self):
        result = services.cancel_order(self.order_id, idempotency_key="cancel-1")
        self.assertEqual(result["status"], "CANCELADO")
        with db.connect() as connection:
            self.assertEqual(tuple(connection.execute("SELECT physical_quantity,reserved_quantity FROM inventory").fetchone()), (1, 0))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 0)

    def test_desfazer_cancelamento_retorna_pendente(self):
        services.cancel_order(self.order_id, idempotency_key="cancel-undo-1")
        result = services.undo_order_action(self.order_id, idempotency_key="undo-cancel-1")
        self.assertEqual(result["status"], "PENDENTE")
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT cancelled_at FROM orders WHERE id=?", (self.order_id,)).fetchone()[0], None)

    def test_desfazer_aprovacao_devolve_estoque(self):
        services.approve_order(self.order_id, idempotency_key="approve-undo-1")
        result = services.undo_order_action(self.order_id, idempotency_key="undo-approve-1")
        self.assertEqual(result["status"], "PENDENTE")
        with db.connect() as connection:
            inventory = connection.execute("SELECT physical_quantity,reserved_quantity FROM inventory WHERE product_id=?", (self.product_id,)).fetchone()
            movement_types = [row[0] for row in connection.execute("SELECT movement_type FROM stock_movements WHERE order_id=? ORDER BY id", (self.order_id,)).fetchall()]
        self.assertEqual(tuple(inventory), (1, 0))
        self.assertEqual(movement_types, ["RESERVA", "BAIXA_PEDIDO", "DEVOLUCAO"])

    def test_desfazer_conclusao_retorna_aprovado(self):
        services.approve_order(self.order_id, idempotency_key="approve-conclude-undo-1")
        services.conclude_order(self.order_id, idempotency_key="conclude-undo-1")
        result = services.undo_order_action(self.order_id, idempotency_key="undo-conclude-1")
        self.assertEqual(result["status"], "APROVADO")
        with db.connect() as connection:
            inventory = connection.execute("SELECT physical_quantity,reserved_quantity FROM inventory WHERE product_id=?", (self.product_id,)).fetchone()
            self.assertIsNone(connection.execute("SELECT concluded_at FROM orders WHERE id=?", (self.order_id,)).fetchone()[0])
        self.assertEqual(tuple(inventory), (0, 0))

    def test_status_invalido_e_bloqueado(self):
        services.approve_order(self.order_id)
        with self.assertRaises(InvalidOrderStatusError):
            services.cancel_order(self.order_id)

    def test_saida_nao_comercial_baixa_e_registra_destinatario(self):
        result = services.withdraw_noncommercial(
            [{"product_id": self.product_id, "quantity": 1}],
            "KIT_INTEGRACAO",
            "Novo colaborador",
            "Integração de agosto",
        )
        self.assertEqual(result["destinatario"], "Novo colaborador")
        with db.connect() as connection:
            inventory = connection.execute("SELECT physical_quantity,reserved_quantity FROM inventory WHERE product_id=?", (self.product_id,)).fetchone()
            movement = connection.execute("SELECT movement_type,reference_type,reason,quantity_after FROM stock_movements WHERE product_id=?", (self.product_id,)).fetchone()
        self.assertEqual(tuple(inventory), (0, 0))
        self.assertEqual(tuple(movement), ("SAIDA", "SAIDA_NAO_COMERCIAL", "Kit de integração: retirada para Novo colaborador. Integração de agosto", 0))

    def test_pedido_da_loja_cria_pendente_sem_baixar_estoque(self):
        result = services.create_store_order(
            [{"product_id": self.product_id, "quantity": 1}],
            {"email": "colaborador@klubi.com.br", "name": "Colaborador Klubi"},
        )
        self.assertEqual(result["status"], "PENDENTE")
        self.assertEqual(result["customer_email"], "colaborador@klubi.com.br")
        with db.connect() as connection:
            inventory = connection.execute("SELECT physical_quantity,reserved_quantity FROM inventory WHERE product_id=?", (self.product_id,)).fetchone()
            order = connection.execute("SELECT source_key FROM orders WHERE id=?", (result["id"],)).fetchone()
        self.assertEqual(tuple(inventory), (1, 0))
        self.assertTrue(order["source_key"].startswith("portal:"))

    def test_concorrencia_so_uma_aprovacao_obtem_a_unidade(self):
        results: list[str] = []
        errors: list[str] = []
        with db.transaction(immediate=True) as connection:
            now = db.utc_now()
            second = connection.execute("INSERT INTO orders(order_number,customer_identifier,customer_name,status,source_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("KLUBI-TEST-2", "b@klubi.com", "B", "PENDENTE", "order:2", now, now)).lastrowid
            connection.execute("INSERT INTO order_items(order_id,product_id,sku_snapshot,product_name_snapshot,quantity,unit_price_cents,total_price_cents,created_at) VALUES (?,?,?,?,?,?,?,?)", (second, self.product_id, "TEST-1", "Camiseta Branca - P", 1, 1700, 1700, now))

        def attempt(order_id):
            try:
                services.approve_order(order_id)
                results.append("ok")
            except InsufficientStockError:
                errors.append("sem-estoque")

        threads = [threading.Thread(target=attempt, args=(self.order_id,)), threading.Thread(target=attempt, args=(second,))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(len(errors), 1)


    # ---------- fotos dos produtos ----------

    PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a") + b"0" * 64).decode()

    def test_foto_vale_para_todos_os_tamanhos_e_aparece_na_loja(self):
        with db.transaction(immediate=True) as connection:
            now = db.utc_now()
            other = connection.execute("INSERT INTO products(sku,name,base_name,size,source_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("TEST-2", "Camiseta Branca - M", "Camiseta Branca", "M", "test:2", now, now)).lastrowid
            connection.execute("INSERT INTO inventory(product_id,physical_quantity,updated_at) VALUES (?,?,?)", (other, 0, now))
        result = services.save_product_image("camiseta branca", "data:image/png;base64," + self.PNG)
        self.assertEqual(result["base_name"], "Camiseta Branca")
        self.assertTrue(result["image_url"].startswith("/imagens/camiseta-branca-"))
        store = services.list_store_products()["items"]
        self.assertEqual({item["image_url"] for item in store}, {result["image_url"]})
        self.assertEqual({item["size"] for item in store}, {"P", "M"})
        self.assertIsNotNone(services.product_image_file(result["image_url"].rsplit("/", 1)[1]))

    def test_trocar_foto_apaga_a_anterior(self):
        first = services.save_product_image("Camiseta Branca", self.PNG)["image_url"].rsplit("/", 1)[1]
        second = services.save_product_image("Camiseta Branca", self.PNG)["image_url"].rsplit("/", 1)[1]
        self.assertNotEqual(first, second)
        self.assertIsNone(services.product_image_file(first))
        self.assertIsNotNone(services.product_image_file(second))
        self.assertEqual(len(services.list_product_images()["items"]), 1)

    def test_remover_foto(self):
        file_name = services.save_product_image("Camiseta Branca", self.PNG)["image_url"].rsplit("/", 1)[1]
        services.remove_product_image("Camiseta Branca")
        self.assertIsNone(services.product_image_file(file_name))
        self.assertIsNone(services.list_product_images()["items"][0]["image_url"])

    def test_foto_rejeita_arquivo_que_nao_e_imagem_e_produto_inexistente(self):
        with self.assertRaises(ValueError):
            services.save_product_image("Camiseta Branca", base64.b64encode(b"<svg onload=alert(1)>").decode())
        with self.assertRaises(ValueError):
            services.save_product_image("Camiseta Branca", "isso nao e base64!")
        with self.assertRaises(ProductNotFoundError):
            services.save_product_image("Produto Fantasma", self.PNG)

    def test_mesmo_produto_em_duas_cores_vira_dois_cartoes_com_fotos_separadas(self):
        with db.transaction(immediate=True) as connection:
            now = db.utc_now()
            for sku, color in (("MOL-B", "BRANCO"), ("MOL-P", "PRETO")):
                pid = connection.execute("INSERT INTO products(sku,name,base_name,color,size,source_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (sku, "Moletom - P", "Moletom", color, "P", sku, now, now)).lastrowid
                connection.execute("INSERT INTO inventory(product_id,physical_quantity,updated_at) VALUES (?,?,?)", (pid, 1, now))
        names = {item["base_name"] for item in services.list_product_images()["items"]}
        self.assertTrue({"Moletom Branco", "Moletom Preto"} <= names)
        services.save_product_image("Moletom Preto", self.PNG)
        store = {item["sku"]: item for item in services.list_store_products()["items"]}
        self.assertIsNone(store["MOL-B"]["image_url"])
        self.assertIsNotNone(store["MOL-P"]["image_url"])
        self.assertEqual(store["MOL-P"]["group_name"], "Moletom Preto")
        self.assertEqual(store["TEST-1"]["group_name"], "Camiseta Branca")

    def test_nome_de_arquivo_de_foto_nao_sai_da_pasta(self):
        for name in ("../test.db", "..%2Ftest.db", "foto.svg", "", "a/b.png"):
            self.assertIsNone(services.product_image_file(name))

if __name__ == "__main__":
    unittest.main()
