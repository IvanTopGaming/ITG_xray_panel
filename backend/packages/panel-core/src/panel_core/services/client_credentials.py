from panel_core.extensions import db
from panel_core.models import Client, ClientCredential


def resolve_client(identifier):
    client = db.session.get(Client, identifier) if identifier else None
    if client is not None:
        return client
    alias = db.session.get(ClientCredential, identifier) if identifier else None
    return alias.client if alias is not None else None


def credentials_for(client):
    return [client, *sorted(client.credential_records, key=lambda row: row.id)]


def credential_aliases(client):
    return [
        {"id": row.id, "email": row.email, "flow": row.flow or "", "wg_address": row.wg_address}
        for row in credentials_for(client)[1:]
    ]


def clients_for_runtime_email(inbound_tag, email):
    clients = Client.query.filter_by(email=email)
    aliases = ClientCredential.query.join(Client).filter(ClientCredential.email == email)
    if inbound_tag:
        clients = clients.filter(Client.inbound_tag == inbound_tag)
        aliases = aliases.filter(Client.inbound_tag == inbound_tag)
    result = {row.id: row for row in clients.all()}
    result.update({row.client_id: row.client for row in aliases.all()})
    return list(result.values())
