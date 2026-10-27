import hashlib
import hmac
import os
import re
import secrets
import unicodedata

import mysql.connector
from mysql.connector import errors, pooling
import streamlit as st

st.set_page_config(page_title="Diamond Guess", page_icon="⚾", layout="centered")

st.markdown("""
<style>
.stApp { background-color: #F6F0E4; color: #182D49; }
.block-container { max-width: 900px; padding-top: 2.5rem; padding-bottom: 3rem; }
h1, h2, h3 { color: #182D49 !important; }
.stApp p, .stApp label { color: #35445A; }

[data-testid="stMetric"] {
    background: #FFFCF5; border: 1px solid #DED3BE; border-top: 4px solid #A83232;
    border-radius: 10px; padding: 16px 14px; box-shadow: 0 3px 8px rgba(24,45,73,0.05);
}
[data-testid="stMetricLabel"] { color: #657087 !important; }
[data-testid="stMetricValue"] { color: #182D49 !important; }

[data-testid="stSelectbox"] [data-baseweb="select"] { background-color: #FFFCF5 !important; }
[data-testid="stSelectbox"] [data-baseweb="select"] [role="button"] { color: #182D49 !important; }
[data-testid="stSelectbox"] [data-baseweb="select"] div[class*="singleValue"],
[data-testid="stSelectbox"] [data-baseweb="select"] div[class*="Value"] {
    color: #182D49 !important; -webkit-text-fill-color: #182D49 !important; opacity: 1 !important;
}
[data-testid="stSelectbox"] svg { color: #182D49 !important; }

[data-testid="stTextInput"] input {
    background-color: #FFFCF5 !important; color: #182D49 !important;
    -webkit-text-fill-color: #182D49 !important; caret-color: #A83232 !important;
}
[data-testid="stTextInput"] input::placeholder {
    color: #657087 !important; -webkit-text-fill-color: #657087 !important; opacity: 1 !important;
}

.stButton > button, .stFormSubmitButton > button {
    background-color: #A83232; color: #FFFFFF !important; border: 1px solid #A83232;
    border-radius: 8px; font-weight: 700; min-height: 44px; cursor: pointer;
}
.stButton > button:hover, .stFormSubmitButton > button:hover {
    background-color: #812424; color: #FFFFFF !important;
}

hr { border-color: #DED3BE; }
[data-testid="stAlert"] { border-radius: 10px; }

.scoreboard {
    background: #182D49; color: #FFF9ED; padding: 20px 24px; border-radius: 12px;
    margin: 12px 0 22px 0; border-bottom: 5px solid #A83232;
}
.scoreboard-label {
    color: #E7D9BC; font-size: 0.78rem; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase;
}
.scoreboard-number { color: #FFFFFF; font-size: 2rem; font-weight: 850; margin-top: 4px; }

.win-panel {
    background: #E7EFE2; border: 1px solid #B9CCAE; border-left: 5px solid #39734A;
    border-radius: 10px; padding: 22px; margin: 16px 0;
}
.win-title { color: #245638; font-size: 1.7rem; font-weight: 850; }
.win-copy { color: #365B41; margin-top: 6px; }

@media (max-width: 600px) {
    .block-container { padding-left: 1rem; padding-right: 1rem; }
    [data-testid="stMetric"] { padding: 10px 8px; }
}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- config

DIFFICULTIES = ["easy", "medium", "hard"]
YEARS = ["2025", "2024"]
BASE_POINTS = {"easy": 100, "medium": 200, "hard": 300}
MAX_WRONG = 5
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


# ---------------------------------------------------------------- name matching

def normalize_name(name):
    name = unicodedata.normalize("NFKD", str(name))
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^\w\s]", " ", name.casefold())
    return " ".join(name.split())


def levenshtein_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (char_a != char_b),
            ))
        previous = current
    return previous[-1]


def is_close_match(guess, answer):
    """Accept the full name or the last name, allowing small typos."""
    guess = normalize_name(guess)
    answer = normalize_name(answer)
    if not guess:
        return False

    core = [p for p in answer.split() if p not in NAME_SUFFIXES]
    last = core[-1] if core else answer

    for target in {answer, last}:
        if guess == target:
            return True
        if len(guess) < 3:
            continue
        allowed = 0 if len(target) <= 4 else 1 if len(target) < 8 else 2
        if levenshtein_distance(guess, target) <= allowed:
            return True
    return False


# ---------------------------------------------------------------- database

@st.cache_resource
def get_pool():
    password = os.environ.get("MYSQL_PASSWORD")
    if not password:
        return None
    config = dict(
        pool_name="diamond_guess",
        pool_size=5,
        host=os.environ.get("MYSQL_HOST", "localhost"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=password,
        database=os.environ.get("MYSQL_DATABASE", "sys"),
    )
    ca_file = os.environ.get("MYSQL_SSL_CA")  # needed by most hosted databases
    if ca_file:
        config["ssl_ca"] = ca_file
    return pooling.MySQLConnectionPool(**config)


def get_connection():
    try:
        pool = get_pool()
        if pool is None:
            st.error("MYSQL_PASSWORD environment variable was not found.")
            return None
        return pool.get_connection()
    except mysql.connector.Error as error:
        st.error(f"Could not connect to the database: {error}")
        return None


def run_query(sql, params=(), *, fetch=None, commit=False, dictionary=False):
    """Run one query. fetch is 'one', 'all' or None. Raises mysql.connector.Error."""
    connection = get_connection()
    if connection is None:
        raise mysql.connector.Error("No database connection")
    cursor = None
    try:
        cursor = connection.cursor(dictionary=dictionary)
        cursor.execute(sql, params)
        result = cursor.rowcount if fetch is None else None
        if fetch == "one":
            result = cursor.fetchone()
        elif fetch == "all":
            result = cursor.fetchall()
        if commit:
            connection.commit()
        return result
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()  # returns the connection to the pool


def get_random_player(difficulty, year):
    if difficulty not in DIFFICULTIES or year not in YEARS:
        return None
    table = f"{difficulty}_{year}"  # built only from validated values
    try:
        return run_query(
            f"SELECT * FROM `{table}` ORDER BY RAND() LIMIT 1",
            fetch="one", dictionary=True,
        )
    except mysql.connector.Error as error:
        st.error(f"Database error: {error}")
        return None


@st.cache_resource
def create_scores_table():
    try:
        run_query("""
            CREATE TABLE IF NOT EXISTS diamond_guess_scores (
                nickname VARCHAR(50) PRIMARY KEY,
                games_played INT NOT NULL DEFAULT 0,
                games_won INT NOT NULL DEFAULT 0,
                total_points INT NOT NULL DEFAULT 0
            )
        """, commit=True)
        try:
            run_query(
                "ALTER TABLE diamond_guess_scores ADD COLUMN password_hash VARCHAR(255) NULL",
                commit=True,
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:  # 1060 = column already exists
                raise
    except mysql.connector.Error as error:
        st.error(f"Could not create scores table: {error}")
    return True


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except ValueError:
        return False


def create_account(nickname, password):
    """Returns the stored nickname, False if the name is taken, None on DB error."""
    pw_hash = hash_password(password)
    try:
        run_query(
            "INSERT INTO diamond_guess_scores (nickname, password_hash) VALUES (%s, %s)",
            (nickname, pw_hash), commit=True,
        )
        return nickname
    except errors.IntegrityError:
        pass
    except mysql.connector.Error as error:
        st.error(f"Database error: {error}")
        return None

    # Name exists. Allow claiming only old accounts that never had a password.
    try:
        claimed = run_query(
            "UPDATE diamond_guess_scores SET password_hash = %s "
            "WHERE nickname = %s AND password_hash IS NULL",
            (pw_hash, nickname), commit=True,
        )
        if not claimed:
            return False
        row = run_query(
            "SELECT nickname FROM diamond_guess_scores WHERE nickname = %s",
            (nickname,), fetch="one",
        )
        return row[0]
    except mysql.connector.Error as error:
        st.error(f"Database error: {error}")
        return None


def check_login(nickname, password):
    """Returns the stored nickname on success, otherwise None."""
    try:
        row = run_query(
            "SELECT nickname, password_hash FROM diamond_guess_scores WHERE nickname = %s",
            (nickname,), fetch="one",
        )
    except mysql.connector.Error as error:
        st.error(f"Database error: {error}")
        return None
    if row and row[1] and verify_password(password, row[1]):
        return row[0]
    return None


def save_game_result(nickname, won, points):
    try:
        run_query("""
            UPDATE diamond_guess_scores
            SET games_played = games_played + 1,
                games_won = games_won + %s,
                total_points = total_points + %s
            WHERE nickname = %s
        """, (int(won), points, nickname), commit=True)
        get_leaderboard.clear()
        return True
    except mysql.connector.Error as error:
        st.error(f"Could not save score: {error}")
        return False


@st.cache_data(ttl=10)
def get_leaderboard():
    try:
        rows = run_query("""
            SELECT nickname, games_played, games_won, total_points,
                   ROUND(100.0 * games_won / NULLIF(games_played, 0), 1) AS win_percentage
            FROM diamond_guess_scores
            ORDER BY total_points DESC, games_won DESC, nickname ASC
            LIMIT 20
        """, fetch="all", dictionary=True)
    except mysql.connector.Error as error:
        st.error(f"Could not load leaderboard: {error}")
        return []
    for row in rows:
        row["win_percentage"] = float(row["win_percentage"] or 0)
    return rows


# ---------------------------------------------------------------- game state

def start_new_game(difficulty, year):
    player = get_random_player(difficulty, year)
    if not player or "Player" not in player:
        st.error("Could not load a player. Check that the tables exist and have a 'Player' column.")
        return False

    st.session_state.update({
        "player_name": str(player.pop("Player")),
        "player_stats": player,
        "game_started": True,
        "attempts": 0,
        "game_over": False,
        "won": False,
        "feedback": "",
        "result_saved": False,
        "celebrated": False,
        "active_difficulty": difficulty,
        "last_game_points": 0,
    })
    return True


def calculate_points(won, difficulty, wrong_guesses):
    if not won:
        return 0
    base = BASE_POINTS.get(difficulty, 100)
    return base * (MAX_WRONG - wrong_guesses) // MAX_WRONG


# ---------------------------------------------------------------- page

create_scores_table()

st.markdown(
    """
    <div style="text-align:center; margin-bottom:24px;">
        <div style="font-size:2.8rem;">⚾</div>
        <h1 style="margin:0;">DIAMOND GUESS</h1>
        <p style="color:#657087; margin-top:6px;">THE ULTIMATE MLB PLAYER CHALLENGE</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.subheader("Player login")

if st.session_state.get("active_nickname"):
    st.caption(f"Playing as **{st.session_state['active_nickname']}**")
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()
else:
    login_tab, signup_tab = st.tabs(["Log in", "Create account"])

    with login_tab:
        with st.form("login_form"):
            login_name = st.text_input("Username", max_chars=50).strip()
            login_pw = st.text_input("Password", type="password")
            login_submit = st.form_submit_button("Log in", use_container_width=True)
        if login_submit:
            name = check_login(login_name, login_pw)
            if name:
                st.session_state["active_nickname"] = name
                st.rerun()
            else:
                st.error("Incorrect username or password.")

    with signup_tab:
        st.caption(
            "New here? Pick a username and password. "
            "Older accounts without a password can be claimed here too."
        )
        with st.form("signup_form"):
            new_name = st.text_input(
                "Choose a username", max_chars=50, placeholder="e.g. Slugger23"
            ).strip()
            new_pw = st.text_input("Choose a password", type="password")
            confirm_pw = st.text_input("Confirm password", type="password")
            signup_submit = st.form_submit_button("Create account", use_container_width=True)
        if signup_submit:
            if len(new_name) < 3:
                st.warning("Username must be at least 3 characters.")
            elif len(new_pw) < 6:
                st.warning("Password must be at least 6 characters.")
            elif new_pw != confirm_pw:
                st.warning("Passwords do not match.")
            else:
                result = create_account(new_name, new_pw)
                if result:
                    st.session_state["active_nickname"] = result
                    get_leaderboard.clear()
                    st.rerun()
                elif result is False:
                    st.error("That username is already taken. Please choose another.")

st.divider()

col1, col2 = st.columns(2)
with col1:
    difficulty = st.selectbox("⚾ DIFFICULTY", DIFFICULTIES, index=0)
with col2:
    year = st.selectbox("📅 SEASON", YEARS, index=0)

if st.button("⚾ Start Game", type="primary", use_container_width=True):
    if not st.session_state.get("active_nickname"):
        st.warning("Log in before starting a game.")
    elif start_new_game(difficulty, year):
        st.rerun()

if st.session_state.get("game_started", False):

    st.markdown(
        """
        <div class="scoreboard">
            <div class="scoreboard-label">Who's the player?</div>
            <div>Study the stats below and make your guess.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    stats_list = list(st.session_state["player_stats"].items())
    for i in range(0, len(stats_list), 3):
        for column, (stat, value) in zip(st.columns(3), stats_list[i:i + 3]):
            with column:
                st.metric(label=stat, value=value)

    attempts = st.session_state["attempts"]
    remaining = max(0, MAX_WRONG - attempts)

    counter_col1, counter_col2 = st.columns(2)
    with counter_col1:
        st.markdown(
            f"""
            <div class="scoreboard">
                <div class="scoreboard-label">Wrong guesses</div>
                <div class="scoreboard-number">{attempts} / {MAX_WRONG}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with counter_col2:
        st.markdown(
            f"""
            <div class="scoreboard">
                <div class="scoreboard-label">Guesses remaining</div>
                <div class="scoreboard-number">{remaining}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    feedback = st.session_state.get("feedback", "")
    if feedback == "incorrect":
        st.error("Not quite! Take another look at the stats.")
    elif feedback == "empty":
        st.warning("Enter a player's name before submitting.")
    elif feedback == "correct":
        st.success("Correct! You know ball!")
    elif feedback in ("gave_up", "out_of_guesses"):
        st.info(f"The player was {st.session_state['player_name']}.")

    if not st.session_state["game_over"]:
        with st.form("guess_form"):
            guess = st.text_input("YOUR GUESS", placeholder="Type the player's name...")
            submitted = st.form_submit_button(
                "Submit Guess ⚾", type="primary", use_container_width=True
            )

        if submitted:
            if not guess.strip():
                st.session_state["feedback"] = "empty"
            elif is_close_match(guess, st.session_state["player_name"]):
                st.session_state.update(feedback="correct", won=True, game_over=True)
            else:
                st.session_state["attempts"] += 1
                if st.session_state["attempts"] >= MAX_WRONG:
                    st.session_state.update(feedback="out_of_guesses", won=False, game_over=True)
                else:
                    st.session_state["feedback"] = "incorrect"
            st.rerun()

        if st.button("🏳️ Give Up", use_container_width=True):
            st.session_state.update(feedback="gave_up", won=False, game_over=True)
            st.rerun()

    else:
        won = st.session_state["won"]

        if not st.session_state["result_saved"]:
            points = calculate_points(
                won,
                st.session_state["active_difficulty"],
                st.session_state["attempts"],
            )
            if save_game_result(st.session_state["active_nickname"], won, points):
                st.session_state["result_saved"] = True
                st.session_state["last_game_points"] = points

        player_name = st.session_state["player_name"]
        points = st.session_state["last_game_points"]

        if won:
            st.markdown(
                f"""
                <div class="win-panel">
                    <div class="win-title">🏆 HOME RUN!</div>
                    <div class="win-copy">
                        It was <strong>{player_name}</strong>. You earned {points} points!
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if not st.session_state["celebrated"]:
                st.balloons()
                st.session_state["celebrated"] = True
        else:
            st.markdown(
                f"""
                <div class="win-panel">
                    <div class="win-title">⚾ GAME OVER</div>
                    <div class="win-copy">
                        The player was <strong>{player_name}</strong>. Better luck next inning!
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        if st.button("↻ Play Again", type="primary", use_container_width=True):
            if start_new_game(difficulty, year):
                st.rerun()

st.divider()
st.header("🏆 Diamond Guess Leaderboard")

leaderboard = get_leaderboard()

if leaderboard:
    st.dataframe(
        leaderboard,
        use_container_width=True,
        hide_index=True,
        column_config={
            "nickname": "Player",
            "games_played": "Games Played",
            "games_won": "Wins",
            "total_points": "Total Points",
            "win_percentage": st.column_config.NumberColumn("Win %", format="%.1f%%"),
        },
    )
else:
    st.info("No scores yet. Play a game to claim the top spot!")

st.markdown(
    """
    <div style="text-align:center; margin-top:32px; padding-top:16px;
                border-top:1px solid #DED3BE; font-size:0.85rem; color:#657087;">
        All data comes from
        <a href="https://www.baseball-reference.com/" target="_blank"
           rel="noopener noreferrer"
           style="color:#A83232; font-weight:700; text-decoration:none;">
            Baseball Reference
        </a>
    </div>
    """,
    unsafe_allow_html=True,
)
