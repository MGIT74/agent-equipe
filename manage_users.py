#!/usr/bin/env python3
"""
Script d'administration des comptes employés de l'Agent Équipe.

Usage :
  python3 manage_users.py add <username> <password> <profile> [display_name]
  python3 manage_users.py list
  python3 manage_users.py remove <username>
  python3 manage_users.py passwd <username> <new_password>

Exemples :
  python3 manage_users.py add marie 'S3cret!' marie-emploi "Marie Dupont"
  python3 manage_users.py list
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "team_users.json"

# Importe les fonctions du serveur
sys.path.insert(0, str(BASE_DIR))
from server import _hash_password, _load_users, _save_users, profile_exists, ensure_profile


def cmd_add(username: str, password: str, profile: str, display_name: str | None):
    import re
    if not re.match(r"^[a-zA-Z0-9_.-]{2,32}$", username):
        print("✗ Identifiant invalide (2-32 caractères : lettres, chiffres, . _ -)")
        sys.exit(1)
    if len(password) < 6:
        print("✗ Mot de passe trop court (6 caractères minimum)")
        sys.exit(1)
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", profile):
        print("✗ Nom de profil invalide (minuscules, chiffres, - _ ; ex : marie)")
        sys.exit(1)

    users = _load_users()
    if username in users:
        print(f"✗ L'identifiant '{username}' existe déjà")
        sys.exit(1)

    print(f"→ Vérification du profil Hermes '{profile}'…")
    if not ensure_profile(profile):
        print(f"✗ Impossible de créer/vérifier le profil Hermes '{profile}'")
        print("  (le serveur Hermes WebUI est-il démarré sur 127.0.0.1:8787 ?)")
        sys.exit(1)

    users[username] = {
        "password_hash": _hash_password(password),
        "profile": profile,
        "display_name": display_name or username,
    }
    _save_users(users)
    print(f"✓ Employé '{username}' créé — profil Hermes : {profile}")
    if display_name:
        print(f"  Nom affiché : {display_name}")


def cmd_list():
    users = _load_users()
    if not users:
        print("(aucun compte employé)")
        return
    print(f"{'IDENTIFIANT':<16} {'PROFIL':<20} {'NOM AFFICHÉ':<24}")
    print("-" * 62)
    for username, u in sorted(users.items()):
        print(f"{username:<16} {u['profile']:<20} {u.get('display_name', ''):<24}")


def cmd_remove(username: str):
    users = _load_users()
    if username not in users:
        print(f"✗ Identifiant '{username}' introuvable")
        sys.exit(1)
    users.pop(username)
    _save_users(users)
    print(f"✓ Employé '{username}' supprimé (le profil Hermes est conservé)")


def cmd_passwd(username: str, new_password: str):
    if len(new_password) < 6:
        print("✗ Mot de passe trop court (6 caractères minimum)")
        sys.exit(1)
    users = _load_users()
    if username not in users:
        print(f"✗ Identifiant '{username}' introuvable")
        sys.exit(1)
    users[username]["password_hash"] = _hash_password(new_password)
    _save_users(users)
    print(f"✓ Mot de passe de '{username}' modifié")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]

    if cmd == "add" and len(sys.argv) >= 5:
        cmd_add(sys.argv[2], sys.argv[3], sys.argv[4],
                sys.argv[5] if len(sys.argv) > 5 else None)
    elif cmd == "list":
        cmd_list()
    elif cmd == "remove" and len(sys.argv) >= 3:
        cmd_remove(sys.argv[2])
    elif cmd == "passwd" and len(sys.argv) >= 4:
        cmd_passwd(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()