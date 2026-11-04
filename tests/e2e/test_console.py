"""What the seller and the account owner do in the console, in a real browser."""
import json
import time

import pyotp
import pytest

pytestmark = [pytest.mark.db, pytest.mark.e2e]


def see(page, text: str) -> None:
    page.get_by_text(text, exact=False).locator("visible=true").first.wait_for(state="visible")


# ------------------------------------------------------------------ access
def test_wrong_password_is_refused_and_the_right_one_opens_the_inbox(visit, login, world):
    page = visit()
    login(page, "seller", "senha-errada-1234")
    see(page, "Credenciais inválidas")
    login(page, "seller")
    see(page, "Cliente ownership")
    assert "LAB-" not in page.content(), "lab conversations stay out of the inbox"


def test_a_screen_without_a_session_goes_to_login(visit, server):
    page = visit()
    page.goto("/console/lgpd")
    assert page.url.endswith("/console/login")


def test_each_role_sees_its_own_screens(visit, login):
    page = visit()
    login(page, "seller")
    see(page, "Conversas")
    nav = page.locator("a.nav").all_inner_texts()
    assert "Posts" in nav and "Guia" in nav and "Revisão" in nav
    assert "LGPD" not in nav and "Configuração" not in nav and "Curadoria" not in nav
    assert page.goto("/console/lgpd").status == 403
    other = visit()
    login(other, "reviewer")
    see(other, "Conversas")
    nav = other.locator("a.nav").all_inner_texts()
    assert "Curadoria" in nav and "Revisão" in nav and "Posts" not in nav


def test_two_step_login(visit, login):
    page = visit()
    login(page, "totp")
    see(page, "Conversas")
    page.goto("/console/totp")
    secret = page.locator("p.muted code").inner_text().strip()
    page.fill('input[name="code"]', pyotp.TOTP(secret).now())
    page.get_by_role("button", name="Ativar verificação").click()
    see(page, "Cliente ownership")
    page.goto("/console/totp")
    see(page, "está ativa")
    page.goto("/console/logout")
    login(page, "totp")
    page.wait_for_url("**/console/login/totp*")
    page.fill('input[name="code"]', "000000")
    page.get_by_role("button", name="Confirmar").click()
    see(page, "inválido")
    page.fill('input[name="code"]', pyotp.TOTP(secret).at(time.time() + 30))    # the code used to enroll cannot be replayed
    page.get_by_role("button", name="Confirmar").click()
    see(page, "Cliente ownership")


def test_changing_the_password(visit, login, world):
    page = visit()
    login(page, "newpass")
    see(page, "Conversas")
    page.goto("/console/password")
    page.fill('input[name="current"]', world.password)
    page.fill('input[name="new"]', "outra-senha-bem-longa")
    page.get_by_role("button", name="Trocar senha").click()
    page.wait_for_load_state()
    page.goto("/console/logout")
    login(page, "newpass")
    see(page, "Credenciais inválidas")
    login(page, "newpass", "outra-senha-bem-longa")
    see(page, "Cliente ownership")


# ------------------------------------------------------------------ conversation
def test_take_pause_and_give_back_a_conversation(visit, login, world, sql):
    page = visit()
    login(page, "seller")
    page.get_by_role("link", name="Cliente ownership").click()
    see(page, "Resposta do assistente para ownership")
    state = lambda: sql("select ownership from conversations where id=%s", world.conv["ownership"])[0]["ownership"]    # noqa: E731
    page.get_by_role("button", name="Assumir conversa").click()
    page.locator('[data-ownership="HUMAN"]').wait_for()
    assert state() == "HUMAN"
    page.get_by_role("button", name="Retomar assistente").click()
    page.locator('[data-ownership="BOT"]').wait_for()
    assert state() == "BOT"
    page.get_by_role("button", name="Pausar assistente nesta conversa").click()
    page.locator('[data-ownership="PAUSED"]').wait_for()
    assert state() == "PAUSED"
    assert sql("select count(*) as n from audit_log where action='conversation.ownership' and target=%s", str(world.conv["ownership"]))[0]["n"] == 3


def test_trace_of_a_turn(visit, login, world):
    page = visit()
    login(page, "seller")
    page.goto(f"/console/conversations/{world.conv['trace']}")
    page.get_by_text("por que respondeu isso").first.click()
    page.get_by_text("ver trace").first.click()
    page.locator("pre", has_text="marca-do-trace").wait_for()


def test_seller_link_from_the_notification_needs_a_click_and_changes_the_conversation(visit, world, sql):
    from agentplatform.core.handoff.notifiers import sign_action
    from agentplatform.settings import get_settings
    s = get_settings()
    secret = (s.console_session_secret.get_secret_value() if s.console_session_secret else "dev").encode()
    page = visit()
    state = lambda: sql("select ownership from conversations where id=%s", world.conv["action"])[0]["ownership"]    # noqa: E731
    page.goto(f"/actions/{sign_action(secret, 'took', world.handoff)}")
    assert state() == "AWAITING_HUMAN", "mail scanners open links: opening changes nothing"
    page.get_by_role("button", name="Assumi o atendimento").click()
    see(page, "A conversa está com você")
    assert state() == "HUMAN"
    page.goto(f"/actions/{sign_action(secret, 'return', world.handoff)}")
    page.get_by_role("button", name="Devolver ao assistente").click()
    see(page, "voltou para o assistente")
    assert state() == "BOT"
    assert page.goto(f"/actions/{sign_action(b'outra-chave', 'took', world.handoff)}").status == 404


def test_daily_review(visit, login, world):
    page = visit()
    login(page, "seller")
    page.get_by_role("link", name="Revisão").click()
    see(page, "Cliente review")
    page.get_by_role("link", name="Cliente review").click()
    page.select_option('select[name="verdict"]', "problem")
    page.select_option('select[name="category"]', "compliance")
    page.fill('textarea[name="note"]', "Falou de um prazo")
    page.get_by_role("button", name="Salvar revisão").click()
    see(page, "Falou de um prazo")
    page.goto("/console/reviews")
    see(page, "Regra de conformidade")
    assert page.locator('[data-kpi="violations"]').inner_text() == "1"
    assert page.locator("tr", has_text="Cliente review").first.get_by_text("Tem problema").count() == 1


# ------------------------------------------------------------------ rights of the data subject
def test_search_correct_export_and_erase(visit, login, world, sql):
    page = visit()
    login(page, "owner")
    see(page, "Conversas")
    page.goto("/console/lgpd")
    search = 'form[action="/console/lgpd/search"] '
    page.fill(search + 'input[name="phone"]', world.phone)
    page.get_by_role("button", name="Buscar").click()
    see(page, "Titular encontrado")
    assert page.input_value('form[action$="/correct"] input[name="name"]') == "Cliente Lgpd da Silva"

    correct = 'form[action$="/correct"] '
    page.fill(correct + 'input[name="phone"]', "123")
    page.get_by_role("button", name="Salvar correção").click()
    see(page, "Telefone inválido")
    page.fill(correct + 'input[name="phone"]', world.phone)
    page.fill(correct + 'input[name="name"]', "Cliente Corrigida")
    page.fill('input[name="slot:city"]', "Campinas")
    page.get_by_role("button", name="Salvar correção").click()
    see(page, "Dados corrigidos")
    assert page.input_value('input[name="slot:city"]') == "Campinas"

    with page.expect_download() as d:
        page.get_by_role("button", name="Exportar dados (JSON)").click()
    with open(d.value.path(), encoding="utf-8") as f:
        data = json.load(f)
    assert data["contact"]["name"] == "Cliente Corrigida" and data["lead"]["profile"]["city"]["value"] == "Campinas"

    page.fill('input[name="confirm"]', "talvez")
    page.get_by_role("button", name="Eliminar dados").click()
    see(page, "para confirmar")
    page.goto("/console/lgpd")
    page.fill(search + 'input[name="phone"]', world.phone)
    page.get_by_role("button", name="Buscar").click()
    page.fill('input[name="confirm"]', "ELIMINAR")
    page.get_by_role("button", name="Eliminar dados").click()
    see(page, "Dados eliminados")
    page.fill(search + 'input[name="phone"]', world.phone)
    page.get_by_role("button", name="Buscar").click()
    see(page, "Nenhum titular encontrado")
    actions = {r["action"] for r in sql("select action from audit_log where tenant_id=%s and action like 'lgpd.%%'", world.tenant)}
    assert {"lgpd.search", "lgpd.correct", "lgpd.export", "lgpd.erase"} <= actions


# ------------------------------------------------------------------ posts
def test_direct_choices_on_the_posts_screen(visit, login, world, sql):
    page = visit()
    login(page, "seller")
    page.get_by_role("link", name="Posts").click()
    see(page, "ainda não atende ninguém novo")
    page.locator('input[name="scope"][value="origin_and_new"]').check()
    page.locator('form[action="/console/posts/direct"]').get_by_role("button", name="Salvar").click()
    see(page, "Atendimento no direct salvo")
    assert sql("select scope from dm_settings where tenant_id=%s", world.tenant)[0]["scope"] == "origin_and_new"

    assert "Não publicadas" in page.content()
    page.fill('textarea[name="questions"]', "Quanto custa? R$ 300")
    page.get_by_role("button", name="Publicar no Instagram").click()
    see(page, "Pergunta pronta")
    page.fill('textarea[name="questions"]', "Como funciona?")
    page.get_by_role("button", name="Publicar no Instagram").click()
    see(page, "Espere a lista")
    assert page.input_value('textarea[name="questions"]') == "Como funciona?", "what was typed stays"


# ------------------------------------------------------------------ guide
def test_guide_from_the_menu(visit, login):
    page = visit()
    login(page, "seller")
    page.get_by_role("link", name="Guia").click()
    see(page, "Guia do vendedor")
    see(page, "Responda pelo Instagram")


# ------------------------------------------------------------------ public form
def test_customer_fills_the_form_once(visit, world, sql):
    page = visit(width=390, height=844)
    page.goto(world.form_path)
    see(page, "Não envie CPF")
    page.locator('input[name="first_name"]').fill("Ana")
    page.locator('input[name="consent"]').check()
    page.get_by_role("button", name="Enviar").click()
    see(page, "Obrigado")
    assert page.goto(world.form_path).status == 410
    see(page, "Link expirado")
    assert sql("select count(*) as n from consents c join leads l on l.contact_id = c.contact_id where l.contact_id=%s and c.purpose='lead_form'", world.contact["review"])[0]["n"] == 1


# ------------------------------------------------------------------ phone
@pytest.mark.parametrize("path", ["/console/", "/console/posts", "/console/comments", "/console/reviews", "/console/help", "/console/conversations/{wide}"])
def test_seller_screens_fit_a_phone(visit, login, world, path):
    page = visit(width=390, height=844)
    login(page, "seller")
    see(page, "Conversas")
    assert page.goto(path.format(wide=world.conv["wide"])).status == 200
    width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert width[0] <= width[1] + 1, f"{path} scrolls sideways on a phone: {width}"


# ------------------------------------------------------------------ what the assistant knows
def pdf(*pages: str) -> dict:
    from tests.integration.helpers import make_pdf
    return {"name": "material-sintetico.pdf", "mimeType": "application/pdf", "buffer": make_pdf(list(pages))}


LINES = "\n".join(f"Regra {i} do material sintetico: o cliente recebe a orientacao completa sobre o procedimento {i} da operacao." for i in range(1, 12))


def test_material_from_upload_to_use(visit, login, run_jobs, world, sql):
    page = visit()
    login(page, "owner")
    page.get_by_role("link", name="Base de conhecimento").click()
    assert page.locator('[data-kpi="approved"]').inner_text() == "0"
    page.set_input_files('input[name="file"]', pdf(LINES, LINES.replace("Regra", "Norma"), "Secao final\n" + LINES.replace("Regra", "Item") + "\nA comissão do consultor aparece aqui e nao pode ser usada em resposta nenhuma ao cliente."))
    page.fill('input[name="title"]', "Material de teste")
    page.get_by_role("button", name="Enviar").click()
    page.locator('[data-state="reading"]').wait_for()

    run_jobs()
    page.reload()
    page.locator('[data-state="in_review"]').wait_for()
    see(page, "Regra 1 do material sintetico")
    doc_id = page.url.rsplit("/", 1)[1].split("?")[0]
    assert sql("select count(*) as n from kb_chunks where document_id=%s and embedding is not null", doc_id)[0]["n"] == 0, "nothing leaves before approval"
    assert sql("select count(*) as n from kb_uploads where document_id=%s", doc_id)[0]["n"] == 0, "the file is not kept"

    page.get_by_role("link", name="Ver só os com alerta").click()
    flagged = page.locator("[data-chunk]", has=page.locator('[data-flag="forbidden_phrase"]')).first
    see(flagged, "comissão")
    flagged.get_by_role("button", name="Tirar este trecho").click()
    see(page, "fora de uso")

    page.goto(f"/console/documents/{doc_id}")
    page.select_option('select[name="visibility"]', "guidance_only")
    page.select_option('select[name="audience"]', "internal")
    page.fill('input[name="version_label"]', "nov/2026")
    page.get_by_role("button", name="Aprovar para uso").click()
    page.locator('[data-state="indexing"]').wait_for()
    run_jobs()
    page.reload()
    page.locator('[data-state="approved"]').wait_for()
    see(page, "por Owner")
    row = sql("select count(*) filter (where embedding is not null) as vectors, count(*) filter (where excluded_at is not null) as out_of_use from kb_chunks where document_id=%s", doc_id)[0]
    assert row["vectors"] > 0 and row["out_of_use"] >= 1
    page.goto("/console/documents")
    assert page.locator('[data-kpi="approved"]').inner_text() == "1"

    page.goto(f"/console/documents/{doc_id}")
    page.get_by_role("button", name="Não usar este documento").click()
    page.locator('[data-state="do_not_use"]').wait_for()


def test_material_with_personal_data_is_blocked(visit, login, run_jobs):
    page = visit()
    login(page, "owner")
    see(page, "Conversas")
    page.goto("/console/documents")
    page.set_input_files('input[name="file"]', pdf(LINES + "\nContato do cliente: maria.teste@example.com"))
    page.get_by_role("button", name="Enviar").click()
    page.locator('[data-state="reading"]').wait_for()
    run_jobs()
    page.reload()
    page.locator('[data-state="blocked_pii"]').wait_for()
    see(page, "Bloqueado")
    assert "maria.teste" not in page.content() and page.get_by_role("button", name="Aprovar para uso").count() == 0
    page.goto("/console/documents")
    page.set_input_files('input[name="file"]', {"name": "Arquivo Proibido de Teste.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 x"})
    page.get_by_role("button", name="Enviar").click()
    see(page, "não pode entrar na base")


def test_answering_a_question_the_assistant_could_not(visit, login, run_jobs, world, sql):
    page = visit()
    login(page, "reviewer")
    page.get_by_role("link", name="Curadoria").click()
    card = page.locator("[data-question]", has_text="Posso pagar com boleto?")
    see(card, "Perguntaram 3 vezes")
    assert card.get_by_role("link", name="ver a conversa").count() == 1
    card.locator('textarea[name="answer"]').fill("A comissão é boa.")
    card.get_by_role("button", name="Salvar resposta").click()
    see(page, "promessa ou termo proibido")
    card = page.locator("[data-question]", has_text="Posso pagar com boleto?")
    assert card.locator('textarea[name="answer"]').input_value() == "A comissão é boa.", "what was typed stays"
    card.locator('textarea[name="answer"]').fill("Sim, com boleto todo mes.")
    card.get_by_role("button", name="Salvar resposta").click()
    see(page, "da próxima vez o assistente responde sozinho")
    assert page.locator("[data-question]", has_text="Posso pagar com boleto?").count() == 0

    page.get_by_role("link", name="Perguntas frequentes").click()
    see(page, "Sim, com boleto todo mes.")
    see(page, "Preparando para uso")
    run_jobs()
    page.reload()
    row = page.locator(".item", has_text="Posso pagar com boleto?").first
    see(row, "Aprovado")
    assert sql("select count(*) as n from faq_variant_vectors where tenant_id=%s", world.tenant)[0]["n"] == 1

    page.get_by_role("link", name="Nova pergunta").click()
    page.fill('input[name="question"]', "Rascunho de pergunta?")
    page.fill('textarea[name="answer"]', "Resposta em rascunho.")
    page.get_by_role("button", name="Salvar rascunho").click()
    see(page, "o assistente ainda não usa")
    page.locator(".item", has_text="Rascunho de pergunta?").first.get_by_role("button", name="Retirar").click()
    see(page, "o assistente não usa mais")


@pytest.mark.parametrize("path", ["/console/documents", "/console/faqs", "/console/curation", "/console/lgpd"])
def test_owner_screens_fit_a_phone(visit, login, path):
    page = visit(width=390, height=844)
    login(page, "owner")
    see(page, "Conversas")
    assert page.goto(path).status == 200
    width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert width[0] <= width[1] + 1, f"{path} scrolls sideways on a phone: {width}"


# ------------------------------------------------------------------ owner
def test_pause_everything_and_resume(visit, login, world, sql):
    page = visit()
    login(page, "owner")
    is_on = lambda: sql("select is_on from kill_switches where tenant_id=%s and scope='tenant'", world.tenant)[0]["is_on"]    # noqa: E731
    page.get_by_role("button", name="Pausar em todas").click()
    page.locator(".alert.bad").first.wait_for()
    assert is_on() is True
    page.get_by_role("button", name="Retomar").first.click()
    page.wait_for_load_state()
    assert is_on() is False


def test_config_change_needs_a_second_person(visit, login, world, sql):
    page = visit()
    login(page, "owner")
    see(page, "Conversas")
    page.goto("/console/config")
    page.get_by_text("Editar o YAML completo").click()
    box = page.locator('textarea[name="yaml_text"]').first
    text = box.input_value()
    assert "sla_minutes: 30" in text
    slug = sql("select slug from tenants where id=%s", world.tenant)[0]["slug"]
    box.fill(text.replace("sla_minutes: 30", "sla_minutes: 31").replace("tenant_slug: dev-consortium", f"tenant_slug: {slug}"))
    page.get_by_role("button", name="Validar e ver diferenças").click()
    see(page, "sla_minutes: 31")
    page.get_by_role("button", name="Propor versão").click()
    see(page, "Aguardando outra pessoa aprovar")
    assert page.get_by_role("button", name="Aprovar").count() == 0, "whoever proposed cannot approve"

    second = visit()
    login(second, "approver")
    see(second, "Conversas")
    second.goto("/console/config")
    second.get_by_role("button", name="Aprovar").click()
    second.wait_for_load_state()
    active = sql("select document from config_versions where tenant_id=%s and status='active'", world.tenant)
    assert active[0]["document"]["handoff"]["sla_minutes"] == 31


def test_changing_the_assistant_without_touching_yaml(visit, login, world, sql):
    """Owner changes the tone in plain fields, sees what changes in words; someone else approves, after bringing the draft up to date if needed."""
    page = visit()
    login(page, "owner")
    see(page, "Conversas")
    page.get_by_role("link", name="Assistente", exact=True).click()
    page.locator('select[name="tone"]').select_option("formal")
    page.locator('textarea[name="business_instructions"]').fill("Atendo por videochamada.")
    page.locator('input[name="note"]').fill("tom formal")
    page.get_by_role("button", name="Criar rascunho").click()
    see(page, "Rascunho criado")
    draft = page.locator("[data-draft]").first
    changes = draft.locator("[data-changes]").inner_text()
    assert "Tom" in changes and "formal" in changes and "Instruções do negócio" in changes and "Atendo por videochamada." in changes
    assert "conversation.persona" not in changes, "in words, not paths"
    assert draft.get_by_role("button", name="Aprovar").count() == 0, "whoever proposed cannot approve"

    other = visit()
    login(other, "approver")
    see(other, "Conversas")
    other.goto("/console/config")
    assert other.locator("[data-draft]").first.get_attribute("data-stale") == "no"
    other.locator("[data-draft]").first.get_by_role("button", name="Aprovar").click()
    other.wait_for_load_state()
    active = sql("select document from config_versions where tenant_id=%s and status='active'", world.tenant)
    assert active[0]["document"]["conversation"]["persona"]["tone"] == "formal"
    width = other.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert width[0] <= width[1]


def test_new_screens_fit_a_phone_and_open_for_the_owner(visit, login, world):
    page = visit(width=390, height=844)
    login(page, "owner")
    see(page, "Conversas")
    for path, text in (("/console/setup", "Preparação"), ("/console/assistant", "Instruções do negócio"), ("/console/assistant/rules", "Diretrizes do pacote"),
                       ("/console/assistant/models", "Modelos de IA"), ("/console/knowledge/test", "Testar a base"), ("/console/users", "Convidar alguém")):
        page.goto(path)
        see(page, text)
        width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert width[0] <= width[1], f"{path} overflows on a phone: {width}"


def test_someone_invited_creates_their_own_password(visit, login, world, sql):
    page = visit()
    login(page, "owner")
    see(page, "Conversas")
    page.goto("/console/users")
    page.locator('input[name="name"]').fill("Pessoa Nova")
    email = f"e2e-{world.tenant.hex[:8]}@example.com"
    page.locator('input[name="email"]').fill(email)
    page.locator('form[action="/console/users/invite"] select[name="role"]').select_option("reviewer")
    page.get_by_role("button", name="Criar convite").click()
    link = page.locator("[data-invite-link]").input_value()
    assert "/console/invite/" in link
    guest = visit()
    guest.goto(link[link.index("/console/invite/"):])
    guest.locator("#password").fill("uma-senha-bem-forte")
    guest.locator("#confirm").fill("uma-senha-bem-forte")
    guest.get_by_role("button", name="Criar acesso").click()
    see(guest, "Entrar")
    guest.locator("#email").fill(email)
    guest.locator("#password").fill("uma-senha-bem-forte")
    guest.locator("button.btn-primary").click()
    guest.wait_for_load_state()
    assert sql("select role from tenant_users where display_name='Pessoa Nova'")[0]["role"] == "reviewer"


# ------------------------------------------------------------------ posts written here (ADR-024)
def test_writing_a_post_stays_a_draft_while_publishing_is_off(visit, login, tmp_path):
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (1080, 1080), (40, 90, 160)).save(buffer, "JPEG")
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(buffer.getvalue())
    wrong = tmp_path / "foto.png"
    wrong.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    page = visit()
    login(page, "seller")
    see(page, "Conversas")
    page.goto("/console/posts")
    page.get_by_role("link", name="Novo post").click()
    see(page, "A publicação está desligada")
    page.locator("textarea[name=caption]").fill("Legenda escrita no navegador")
    page.locator("input[name=files]").set_input_files(str(wrong))
    page.get_by_role("button", name="Salvar rascunho").click()
    see(page, "Use JPEG para imagem")

    page.locator("input[name=files]").set_input_files(str(photo))
    page.get_by_role("button", name="Salvar rascunho").click()
    see(page, "Rascunho salvo")
    assert page.locator("textarea[name=caption]").input_value() == "Legenda escrita no navegador"
    preview = page.locator('img[src*="/files/"]').first
    preview.wait_for(state="visible")
    assert preview.evaluate("e => e.complete && e.naturalWidth") == 1080, "the page policy must let the preview load"
    assert page.get_by_role("button", name="Aprovar e publicar agora").is_disabled()
    assert page.get_by_role("button", name="Aprovar e agendar").is_disabled()
    draft_url = page.url.split("?")[0]

    page.goto("/console/posts")
    see(page, "Escritos aqui")
    see(page, "Legenda escrita no navegador")

    page.goto(draft_url)
    page.on("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Excluir rascunho").click()
    see(page, "Rascunho excluído")
