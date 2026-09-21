def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200


def test_unauthenticated_dashboard_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_authenticated_dashboard_renders(auth_client):
    assert auth_client.get("/").status_code == 200
