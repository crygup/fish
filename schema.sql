-- Accounts, privacy and access

CREATE TABLE public.accounts (
    user_id bigint NOT NULL,
    lastfm text,
    lastfm_session_key text,
    steam text,
    roblox text,
    genshin text,
    letterboxd text,
    spotify text,
    spotify_refresh_token text,
    anilist text,
    anilist_access_token text,
    CONSTRAINT accounts_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.banned_ips (
    ip text NOT NULL,
    banned_at timestamp with time zone DEFAULT now(),
    CONSTRAINT banned_ips_pkey PRIMARY KEY (ip)
);

CREATE TABLE public.global_user_blocks (
    user_id bigint NOT NULL,
    blocked_by bigint NOT NULL,
    blocked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT global_user_blocks_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.guild_hourly_post_blocks (
    guild_id bigint NOT NULL,
    user_id bigint NOT NULL,
    blocked_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_hourly_post_blocks_pkey PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX guild_hourly_post_blocks_user_idx ON public.guild_hourly_post_blocks USING btree (user_id, guild_id);

CREATE TABLE public.guild_opted_out (
    guild_id bigint NOT NULL,
    items text[],
    CONSTRAINT guild_opted_out_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.guild_settings (
    guild_id bigint NOT NULL,
    auto_download bigint,
    auto_upload bigint,
    auto_upload_images boolean DEFAULT true NOT NULL,
    auto_upload_gifs boolean DEFAULT true NOT NULL,
    auto_upload_videos boolean DEFAULT true NOT NULL,
    poketwo boolean DEFAULT false,
    poketwo_channel bigint,
    auto_reactions boolean DEFAULT false,
    auto_reactions_channel bigint,
    pinboard bigint,
    mudae_auto_scrape_series boolean DEFAULT false NOT NULL,
    mudae_recent_claims boolean DEFAULT true NOT NULL,
    dehoist boolean DEFAULT false,
    snipe_enabled boolean DEFAULT false NOT NULL,
    editsnipe_enabled boolean DEFAULT false NOT NULL,
    tracking_enabled boolean DEFAULT true NOT NULL,
    history_public boolean DEFAULT true NOT NULL,
    CONSTRAINT guild_settings_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.mudae_dm_consent (
    user_id bigint NOT NULL,
    consented boolean DEFAULT true NOT NULL,
    CONSTRAINT mudae_dm_consent_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.oauth_states (
    state_hash text NOT NULL,
    code_verifier text NOT NULL,
    redirect_uri text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT oauth_states_pkey PRIMARY KEY (state_hash)
);
CREATE INDEX oauth_states_expires_at_idx ON public.oauth_states USING btree (expires_at);

CREATE TABLE public.opted_out (
    user_id bigint NOT NULL,
    items text[],
    CONSTRAINT opted_out_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.phone_consent (
    user_id bigint NOT NULL,
    consented boolean NOT NULL,
    CONSTRAINT phone_consent_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.post_library_blocks (
    user_id bigint NOT NULL,
    blocked_uploader_id bigint NOT NULL,
    blocked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT post_library_blocks_not_self CHECK ((user_id <> blocked_uploader_id)),
    CONSTRAINT post_library_blocks_pkey PRIMARY KEY (user_id, blocked_uploader_id)
);
CREATE INDEX post_library_blocks_uploader_idx ON public.post_library_blocks USING btree (blocked_uploader_id, user_id);

CREATE TABLE public.post_upload_blocks (
    user_id bigint NOT NULL,
    blocked_by bigint NOT NULL,
    blocked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT post_upload_blocks_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.social_user_settings (
    user_id bigint NOT NULL,
    friend_requests_enabled boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT social_user_settings_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.user_settings (
    user_id bigint NOT NULL,
    timezone text,
    anilist_default_media text DEFAULT 'anime'::text NOT NULL,
    currency_tracking_enabled boolean DEFAULT true NOT NULL,
    tracking_enabled boolean DEFAULT true NOT NULL,
    history_public boolean DEFAULT false NOT NULL,
    first_use_notice_shown boolean DEFAULT false NOT NULL,
    wordle_hard_mode boolean DEFAULT false NOT NULL,
    wordle_colourblind_mode boolean DEFAULT false NOT NULL,
    game_tracking_enabled boolean DEFAULT true NOT NULL,
    game_history_public boolean DEFAULT true NOT NULL,
    tracking_consent boolean DEFAULT false NOT NULL,
    CONSTRAINT user_settings_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.video_library_blocks (
    user_id bigint NOT NULL,
    blocked_uploader_id bigint NOT NULL,
    blocked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_library_blocks_not_self CHECK ((user_id <> blocked_uploader_id)),
    CONSTRAINT video_library_blocks_pkey PRIMARY KEY (user_id, blocked_uploader_id)
);
CREATE INDEX video_library_blocks_uploader_idx ON public.video_library_blocks USING btree (blocked_uploader_id, user_id);

CREATE TABLE public.video_upload_blocks (
    user_id bigint NOT NULL,
    blocked_by bigint NOT NULL,
    blocked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_upload_blocks_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.web_sessions (
    session_id_hash text NOT NULL,
    user_id bigint NOT NULL,
    discord_access_token text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT web_sessions_pkey PRIMARY KEY (session_id_hash)
);
CREATE INDEX web_sessions_expires_at_idx ON public.web_sessions USING btree (expires_at);

-- Currency, shop catalogs and ownership

CREATE TABLE public.badge_catalog (
    badge_key text NOT NULL,
    category text NOT NULL,
    display_name text NOT NULL,
    emoji_name text NOT NULL,
    emoji_id bigint,
    is_custom boolean DEFAULT false NOT NULL,
    unicode boolean DEFAULT true NOT NULL,
    animated boolean DEFAULT false NOT NULL,
    price bigint,
    enabled boolean DEFAULT true NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT badge_catalog_category_check CHECK ((category = ANY (ARRAY['stat'::text, 'purchase'::text]))),
    CONSTRAINT badge_catalog_check CHECK (((category = 'stat'::text) OR (price IS NOT NULL))),
    CONSTRAINT badge_catalog_price_check CHECK (((price IS NULL) OR (price > 0))),
    CONSTRAINT badge_catalog_pkey PRIMARY KEY (badge_key)
);

CREATE TABLE public.birthday_rewards (
    user_id bigint NOT NULL,
    last_awarded_on date NOT NULL,
    CONSTRAINT birthday_rewards_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.color_catalog (
    color_key text NOT NULL,
    display_name text NOT NULL,
    hex_value text NOT NULL,
    price bigint NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT color_catalog_price_check CHECK ((price > 0)),
    CONSTRAINT color_catalog_pkey PRIMARY KEY (color_key)
);

CREATE TABLE public.currency_wallets (
    user_id bigint NOT NULL,
    balance bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT currency_wallets_balance_check CHECK (((balance >= 0) AND (balance <= '9000000000000000000'::bigint))),
    CONSTRAINT currency_wallets_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.currency_claims (
    user_id bigint NOT NULL,
    claim_type text NOT NULL,
    period_start date NOT NULL,
    base_amount bigint NOT NULL,
    bonus_amount bigint DEFAULT 0 NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL,
    streak integer DEFAULT 0 NOT NULL,
    streak_bonus_amount bigint DEFAULT 0 NOT NULL,
    CONSTRAINT currency_claims_base_amount_check CHECK ((base_amount > 0)),
    CONSTRAINT currency_claims_bonus_amount_check CHECK ((bonus_amount >= 0)),
    CONSTRAINT currency_claims_claim_type_check CHECK ((claim_type = ANY (ARRAY['daily'::text, 'weekly'::text]))),
    CONSTRAINT currency_claims_streak_bonus_amount_check CHECK ((streak_bonus_amount >= 0)),
    CONSTRAINT currency_claims_streak_check CHECK ((streak >= 0)),
    CONSTRAINT currency_claims_pkey PRIMARY KEY (user_id, claim_type, period_start),
    CONSTRAINT currency_claims_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id) ON DELETE CASCADE
);

CREATE TABLE public.currency_daily_rewards (
    user_id bigint NOT NULL,
    source text NOT NULL,
    period_start date NOT NULL,
    amount bigint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT currency_daily_rewards_amount_check CHECK ((amount >= 0)),
    CONSTRAINT currency_daily_rewards_pkey PRIMARY KEY (user_id, source, period_start),
    CONSTRAINT currency_daily_rewards_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id) ON DELETE CASCADE
);

CREATE TABLE public.currency_gambling_stats (
    user_id bigint NOT NULL,
    total_wagered bigint DEFAULT 0 NOT NULL,
    total_earned bigint DEFAULT 0 NOT NULL,
    total_lost bigint DEFAULT 0 NOT NULL,
    wins bigint DEFAULT 0 NOT NULL,
    losses bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT currency_gambling_stats_losses_check CHECK ((losses >= 0)),
    CONSTRAINT currency_gambling_stats_total_earned_check CHECK ((total_earned >= 0)),
    CONSTRAINT currency_gambling_stats_total_lost_check CHECK ((total_lost >= 0)),
    CONSTRAINT currency_gambling_stats_total_wagered_check CHECK ((total_wagered >= 0)),
    CONSTRAINT currency_gambling_stats_wins_check CHECK ((wins >= 0)),
    CONSTRAINT currency_gambling_stats_pkey PRIMARY KEY (user_id),
    CONSTRAINT currency_gambling_stats_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id) ON DELETE CASCADE
);
CREATE INDEX currency_gambling_stats_wins_idx ON public.currency_gambling_stats USING btree (wins DESC, user_id);

CREATE TABLE public.currency_transactions (
    id bigserial NOT NULL,
    user_id bigint NOT NULL,
    amount bigint NOT NULL,
    source text NOT NULL,
    reference_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT currency_transactions_amount_check CHECK ((amount <> 0)),
    CONSTRAINT currency_transactions_pkey PRIMARY KEY (id),
    CONSTRAINT currency_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX currency_transactions_reference_idx ON public.currency_transactions USING btree (user_id, reference_key) WHERE (reference_key IS NOT NULL);
CREATE INDEX currency_transactions_user_created_idx ON public.currency_transactions USING btree (user_id, created_at DESC);

CREATE TABLE public.currency_wagers (
    id bigserial NOT NULL,
    user_id bigint NOT NULL,
    source text NOT NULL,
    stake bigint,
    status text DEFAULT 'open'::text NOT NULL,
    payout bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    settled_at timestamp with time zone,
    CONSTRAINT currency_wagers_check CHECK ((((status = 'open'::text) AND (payout = 0) AND (settled_at IS NULL)) OR ((status = 'lost'::text) AND (payout = 0) AND (settled_at IS NOT NULL)) OR ((status = 'cashed_out'::text) AND (payout > 0) AND (settled_at IS NOT NULL)))),
    CONSTRAINT currency_wagers_payout_check CHECK (((payout >= 0) AND (payout <= '1000000000000000000'::bigint))),
    CONSTRAINT currency_wagers_status_check CHECK ((status = ANY (ARRAY['open'::text, 'lost'::text, 'cashed_out'::text]))),
    CONSTRAINT currency_wagers_pkey PRIMARY KEY (id),
    CONSTRAINT currency_wagers_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id) ON DELETE CASCADE
);
ALTER TABLE public.currency_wagers
    ADD CONSTRAINT currency_wagers_stake_limit_check CHECK ((stake >= 10)) NOT VALID;
CREATE INDEX currency_wagers_user_created_idx ON public.currency_wagers USING btree (user_id, created_at DESC);

CREATE TABLE public.lottery_rounds (
    round_start timestamp with time zone NOT NULL,
    prize_pool bigint DEFAULT 1000 NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    winning_ticket character(6),
    winner_user_id bigint,
    drawn_at timestamp with time zone,
    CONSTRAINT lottery_rounds_check CHECK ((((status = 'open'::text) AND (winning_ticket IS NULL) AND (winner_user_id IS NULL) AND (drawn_at IS NULL)) OR ((status = 'no_winner'::text) AND (winning_ticket IS NULL) AND (winner_user_id IS NULL) AND (drawn_at IS NOT NULL)) OR ((status = 'drawn'::text) AND (winning_ticket IS NOT NULL) AND (drawn_at IS NOT NULL)))),
    CONSTRAINT lottery_rounds_prize_pool_check CHECK (((prize_pool >= 1000) AND (prize_pool <= '9000000000000000000'::bigint))),
    CONSTRAINT lottery_rounds_status_check CHECK ((status = ANY (ARRAY['open'::text, 'drawn'::text, 'no_winner'::text]))),
    CONSTRAINT lottery_rounds_pkey PRIMARY KEY (round_start),
    CONSTRAINT lottery_rounds_winner_user_id_fkey FOREIGN KEY (winner_user_id) REFERENCES public.currency_wallets(user_id) ON DELETE SET NULL
);

CREATE TABLE public.lottery_tickets (
    id bigserial NOT NULL,
    round_start timestamp with time zone NOT NULL,
    user_id bigint NOT NULL,
    ticket_digits character(6) NOT NULL,
    purchased_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lottery_tickets_ticket_digits_check CHECK ((ticket_digits ~ '^[0-9]{6}$'::text)),
    CONSTRAINT lottery_tickets_pkey PRIMARY KEY (id),
    CONSTRAINT lottery_tickets_round_start_ticket_digits_key UNIQUE (round_start, ticket_digits),
    CONSTRAINT lottery_tickets_round_start_fkey FOREIGN KEY (round_start) REFERENCES public.lottery_rounds(round_start) ON DELETE CASCADE,
    CONSTRAINT lottery_tickets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id) ON DELETE CASCADE
);
CREATE INDEX lottery_tickets_user_round_idx ON public.lottery_tickets USING btree (user_id, round_start);

CREATE TABLE public.ring_catalog (
    ring_key text NOT NULL,
    display_name text NOT NULL,
    emoji_name text NOT NULL,
    emoji_id bigint,
    unicode boolean DEFAULT true NOT NULL,
    animated boolean DEFAULT false NOT NULL,
    display text NOT NULL,
    price bigint NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ring_catalog_price_check CHECK ((price > 0)),
    CONSTRAINT ring_catalog_pkey PRIMARY KEY (ring_key)
);

CREATE TABLE public.title_catalog (
    title_key text NOT NULL,
    display_name text NOT NULL,
    price bigint NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    category text DEFAULT 'Misc titles'::text NOT NULL,
    CONSTRAINT title_catalog_price_check CHECK ((price > 0)),
    CONSTRAINT title_catalog_pkey PRIMARY KEY (title_key)
);

CREATE TABLE public.user_badge_orders (
    user_id bigint NOT NULL,
    badge_keys text[] DEFAULT ARRAY[]::text[] NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_badge_orders_pkey PRIMARY KEY (user_id)
);
CREATE INDEX user_badge_orders_updated_idx ON public.user_badge_orders USING btree (updated_at DESC);

CREATE TABLE public.user_badges (
    id bigserial NOT NULL,
    user_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    emoji_name text NOT NULL,
    emoji_id bigint,
    is_custom boolean NOT NULL,
    unicode boolean DEFAULT false NOT NULL,
    animated boolean DEFAULT false NOT NULL,
    badge_key text DEFAULT 'custom'::text NOT NULL,
    text text NOT NULL,
    badge_source text DEFAULT 'owner'::text NOT NULL,
    catalog_key text,
    purchase_price bigint DEFAULT 0 NOT NULL,
    purchase_guild_id bigint,
    active boolean DEFAULT true NOT NULL,
    revoked_at timestamp with time zone,
    refund_amount bigint DEFAULT 0 NOT NULL,
    revocation_reason text,
    CONSTRAINT user_badges_badge_source_check CHECK ((badge_source = ANY (ARRAY['owner'::text, 'stat'::text, 'purchase'::text]))),
    CONSTRAINT user_badges_check CHECK (((is_custom AND (emoji_id IS NOT NULL) AND (NOT unicode)) OR ((NOT is_custom) AND (emoji_id IS NULL) AND unicode))),
    CONSTRAINT user_badges_purchase_price_check CHECK ((purchase_price >= 0)),
    CONSTRAINT user_badges_refund_amount_check CHECK ((refund_amount >= 0)),
    CONSTRAINT user_badges_pkey PRIMARY KEY (id)
);
CREATE INDEX user_badges_active_idx ON public.user_badges USING btree (user_id, id DESC) WHERE active;
CREATE INDEX user_badges_catalog_idx ON public.user_badges USING btree (catalog_key, active, id DESC);
CREATE INDEX user_badges_user_idx ON public.user_badges USING btree (user_id, id DESC);
CREATE UNIQUE INDEX user_badges_user_key_idx ON public.user_badges USING btree (user_id, badge_key);

CREATE TABLE public.user_colors (
    user_id bigint NOT NULL,
    color_key text NOT NULL,
    hex_value text NOT NULL,
    purchase_price bigint NOT NULL,
    active boolean DEFAULT true NOT NULL,
    equipped boolean DEFAULT false NOT NULL,
    purchased_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_colors_purchase_price_check CHECK ((purchase_price > 0)),
    CONSTRAINT user_colors_pkey PRIMARY KEY (user_id, color_key),
    CONSTRAINT user_colors_color_key_fkey FOREIGN KEY (color_key) REFERENCES public.color_catalog(color_key),
    CONSTRAINT user_colors_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id)
);
CREATE INDEX user_colors_active_idx ON public.user_colors USING btree (user_id, purchased_at DESC) WHERE active;
CREATE UNIQUE INDEX user_colors_one_equipped_idx ON public.user_colors USING btree (user_id) WHERE (active AND equipped);

CREATE TABLE public.user_racing_emojis (
    user_id bigint NOT NULL,
    emoji_key text NOT NULL,
    emoji_name text NOT NULL,
    emoji_id bigint,
    unicode boolean DEFAULT true NOT NULL,
    animated boolean DEFAULT false NOT NULL,
    display text NOT NULL,
    category text NOT NULL,
    purchase_price bigint NOT NULL,
    purchase_guild_id bigint,
    active boolean DEFAULT true NOT NULL,
    equipped boolean DEFAULT false NOT NULL,
    purchased_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_racing_emojis_purchase_price_check CHECK ((purchase_price > 0)),
    CONSTRAINT user_racing_emojis_pkey PRIMARY KEY (user_id, emoji_key),
    CONSTRAINT user_racing_emojis_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id)
);
CREATE INDEX user_racing_emojis_active_idx ON public.user_racing_emojis USING btree (user_id, purchased_at DESC) WHERE active;
CREATE UNIQUE INDEX user_racing_emojis_one_equipped_idx ON public.user_racing_emojis USING btree (user_id) WHERE (active AND equipped);

CREATE TABLE public.user_rings (
    user_id bigint NOT NULL,
    ring_key text NOT NULL,
    quantity bigint DEFAULT 0 NOT NULL,
    equipped_count bigint DEFAULT 0 NOT NULL,
    purchased_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_rings_check CHECK (((equipped_count = ANY (ARRAY[(0)::bigint, (1)::bigint])) AND (equipped_count <= quantity))),
    CONSTRAINT user_rings_quantity_check CHECK ((quantity >= 0)),
    CONSTRAINT user_rings_pkey PRIMARY KEY (user_id, ring_key),
    CONSTRAINT user_rings_ring_key_fkey FOREIGN KEY (ring_key) REFERENCES public.ring_catalog(ring_key),
    CONSTRAINT user_rings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.currency_wallets(user_id)
);
CREATE INDEX user_rings_inventory_idx ON public.user_rings USING btree (user_id, updated_at DESC) WHERE (quantity > 0);
CREATE UNIQUE INDEX user_rings_one_equipped_idx ON public.user_rings USING btree (user_id) WHERE (equipped_count > 0);

CREATE TABLE public.user_titles (
    user_id bigint NOT NULL,
    title_key text NOT NULL,
    text text NOT NULL,
    purchase_price bigint NOT NULL,
    active boolean DEFAULT true NOT NULL,
    purchased_at timestamp with time zone DEFAULT now() NOT NULL,
    equipped boolean DEFAULT false NOT NULL,
    CONSTRAINT user_titles_purchase_price_check CHECK ((purchase_price > 0)),
    CONSTRAINT user_titles_pkey PRIMARY KEY (user_id, title_key),
    CONSTRAINT user_titles_title_key_fkey FOREIGN KEY (title_key) REFERENCES public.title_catalog(title_key)
);
CREATE INDEX user_titles_active_idx ON public.user_titles USING btree (user_id, purchased_at DESC) WHERE active;
CREATE UNIQUE INDEX user_titles_one_equipped_idx ON public.user_titles USING btree (user_id) WHERE (active AND equipped);

-- Profiles, social and reputation

CREATE TABLE public.guild_rep (
    guild_id bigint NOT NULL,
    count bigint DEFAULT 0 NOT NULL,
    CONSTRAINT guild_rep_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.message_xp (
    id serial NOT NULL,
    user_id bigint NOT NULL,
    messages bigint,
    xp bigint,
    CONSTRAINT message_xp_pkey PRIMARY KEY (user_id)
);
CREATE INDEX message_xp_leaderboard_idx ON public.message_xp USING btree (xp DESC, user_id);

CREATE TABLE public.reputation_events (
    id bigserial NOT NULL,
    giver_id bigint NOT NULL,
    receiver_id bigint,
    guild_id bigint,
    kind text NOT NULL,
    source text NOT NULL,
    source_message_id bigint,
    period_start date NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT reputation_events_check CHECK ((((kind = 'user'::text) AND (receiver_id IS NOT NULL)) OR ((kind = 'guild'::text) AND (receiver_id IS NULL) AND (guild_id IS NOT NULL)))),
    CONSTRAINT reputation_events_kind_check CHECK ((kind = ANY (ARRAY['user'::text, 'guild'::text]))),
    CONSTRAINT reputation_events_source_check CHECK ((source = ANY (ARRAY['fishie'::text, 'tatsu'::text]))),
    CONSTRAINT reputation_events_pkey PRIMARY KEY (id)
);
CREATE UNIQUE INDEX reputation_events_fishie_period_idx ON public.reputation_events USING btree (giver_id, kind, period_start) WHERE (source = 'fishie'::text);
CREATE INDEX reputation_events_giver_idx ON public.reputation_events USING btree (giver_id, created_at);
CREATE INDEX reputation_events_guild_idx ON public.reputation_events USING btree (guild_id, created_at);
CREATE INDEX reputation_events_receiver_idx ON public.reputation_events USING btree (receiver_id, created_at);
CREATE UNIQUE INDEX reputation_events_source_message_idx ON public.reputation_events USING btree (source_message_id) WHERE (source_message_id IS NOT NULL);

CREATE TABLE public.social_follows (
    follower_id bigint NOT NULL,
    followed_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT social_follows_check CHECK ((follower_id <> followed_id)),
    CONSTRAINT social_follows_pkey PRIMARY KEY (follower_id, followed_id)
);
CREATE INDEX social_follows_followed_idx ON public.social_follows USING btree (followed_id, created_at DESC);

CREATE TABLE public.social_friend_requests (
    requester_id bigint NOT NULL,
    recipient_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT social_friend_requests_check CHECK ((requester_id <> recipient_id)),
    CONSTRAINT social_friend_requests_pkey PRIMARY KEY (requester_id, recipient_id)
);
CREATE INDEX social_friend_requests_recipient_idx ON public.social_friend_requests USING btree (recipient_id, created_at DESC);

CREATE TABLE public.social_friendships (
    user_low_id bigint NOT NULL,
    user_high_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT social_friendships_check CHECK ((user_low_id < user_high_id)),
    CONSTRAINT social_friendships_pkey PRIMARY KEY (user_low_id, user_high_id)
);
CREATE INDEX social_friendships_high_idx ON public.social_friendships USING btree (user_high_id, created_at DESC);

CREATE TABLE public.social_marriage_cooldowns (
    user_id bigint NOT NULL,
    available_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT social_marriage_cooldowns_pkey PRIMARY KEY (user_id)
);
CREATE INDEX social_marriage_cooldowns_available_idx ON public.social_marriage_cooldowns USING btree (available_at);

CREATE TABLE public.social_marriages (
    id bigserial NOT NULL,
    proposer_id bigint NOT NULL,
    recipient_id bigint NOT NULL,
    ring_key text NOT NULL,
    married_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT social_marriages_check CHECK ((proposer_id <> recipient_id)),
    CONSTRAINT social_marriages_pkey PRIMARY KEY (id),
    CONSTRAINT social_marriages_ring_key_fkey FOREIGN KEY (ring_key) REFERENCES public.ring_catalog(ring_key)
);

CREATE TABLE public.social_marriage_members (
    user_id bigint NOT NULL,
    marriage_id bigint NOT NULL,
    CONSTRAINT social_marriage_members_pkey PRIMARY KEY (user_id),
    CONSTRAINT social_marriage_members_marriage_id_fkey FOREIGN KEY (marriage_id) REFERENCES public.social_marriages(id) ON DELETE CASCADE
);
CREATE INDEX social_marriage_members_marriage_idx ON public.social_marriage_members USING btree (marriage_id);

CREATE TABLE public.user_birthdays (
    user_id bigint NOT NULL,
    month smallint NOT NULL,
    day smallint NOT NULL,
    year smallint,
    CONSTRAINT user_birthdays_check CHECK ((EXTRACT(month FROM make_date(COALESCE((year)::integer, 2000), (month)::integer, (day)::integer)) = (month)::numeric)),
    CONSTRAINT user_birthdays_day_check CHECK (((day >= 1) AND (day <= 31))),
    CONSTRAINT user_birthdays_month_check CHECK (((month >= 1) AND (month <= 12))),
    CONSTRAINT user_birthdays_year_check CHECK (((year >= 1) AND (year <= 9999))),
    CONSTRAINT user_birthdays_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.user_profiles (
    user_id bigint NOT NULL,
    description text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_profiles_description_length CHECK (((description IS NULL) OR (char_length(description) <= 500))),
    CONSTRAINT user_profiles_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.user_rep (
    id serial NOT NULL,
    user_id bigint,
    count integer
);
CREATE UNIQUE INDEX user_rep_user_idx ON public.user_rep USING btree (user_id);

CREATE TABLE public.user_rep_logs (
    id serial NOT NULL,
    user_id bigint,
    author_id bigint,
    value boolean,
    comment text,
    guild_id bigint,
    source_message_id bigint,
    created_at timestamp with time zone DEFAULT now()
);
CREATE UNIQUE INDEX user_rep_logs_source_message_idx ON public.user_rep_logs USING btree (source_message_id) WHERE (source_message_id IS NOT NULL);

-- Games and clicks

CREATE TABLE public.caught_fish (
    user_id bigint NOT NULL,
    bass bigint,
    commandtuna bigint,
    salmon bigint,
    caught_fish bigint,
    carp bigint,
    trout bigint,
    sardine bigint,
    blue_tang bigint,
    pike bigint,
    mackerel bigint,
    red_snapper bigint,
    shark bigint,
    hammerhead_shark bigint,
    great_white_shark bigint,
    leviathan bigint,
    kraken bigint,
    CONSTRAINT caught_fish_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.click_guild_totals (
    guild_id bigint NOT NULL,
    clicks bigint DEFAULT 0 NOT NULL,
    CONSTRAINT click_guild_totals_clicks_check CHECK ((clicks >= 0)),
    CONSTRAINT click_guild_totals_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.click_totals (
    id boolean DEFAULT true NOT NULL,
    clicks bigint DEFAULT 0 NOT NULL,
    CONSTRAINT click_totals_clicks_check CHECK ((clicks >= 0)),
    CONSTRAINT click_totals_id_check CHECK (id),
    CONSTRAINT click_totals_pkey PRIMARY KEY (id)
);
INSERT INTO public.click_totals (id, clicks) VALUES (true, 0);

CREATE TABLE public.click_user_guild_totals (
    user_id bigint NOT NULL,
    guild_id bigint NOT NULL,
    clicks bigint DEFAULT 0 NOT NULL,
    CONSTRAINT click_user_guild_totals_clicks_check CHECK ((clicks >= 0)),
    CONSTRAINT click_user_guild_totals_pkey PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE public.click_user_totals (
    user_id bigint NOT NULL,
    clicks bigint DEFAULT 0 NOT NULL,
    CONSTRAINT click_user_totals_clicks_check CHECK ((clicks >= 0)),
    CONSTRAINT click_user_totals_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.connectfour_games (
    id bigserial NOT NULL,
    guild_id bigint,
    channel_id bigint NOT NULL,
    player_yellow_id bigint NOT NULL,
    player_red_id bigint NOT NULL,
    winner_id bigint,
    loser_id bigint,
    against_bot boolean DEFAULT false NOT NULL,
    bot_difficulty text,
    started_by_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone DEFAULT now() NOT NULL,
    move_count integer DEFAULT 0 NOT NULL,
    move_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT connectfour_bot_difficulty_check CHECK (((against_bot AND (bot_difficulty = ANY (ARRAY['easy'::text, 'normal'::text, 'hard'::text]))) OR ((NOT against_bot) AND (bot_difficulty IS NULL)))),
    CONSTRAINT connectfour_loser_player_check CHECK (((loser_id IS NULL) OR ((loser_id = player_yellow_id) OR (loser_id = player_red_id)))),
    CONSTRAINT connectfour_move_count_check CHECK ((move_count >= 0)),
    CONSTRAINT connectfour_winner_loser_check CHECK (((winner_id IS NULL) OR (loser_id IS NULL) OR (winner_id <> loser_id))),
    CONSTRAINT connectfour_winner_player_check CHECK (((winner_id IS NULL) OR ((winner_id = player_yellow_id) OR (winner_id = player_red_id)))),
    CONSTRAINT connectfour_games_pkey PRIMARY KEY (id)
);
CREATE INDEX connectfour_games_difficulty_winner_idx ON public.connectfour_games USING btree (bot_difficulty, winner_id) WHERE against_bot;
CREATE INDEX connectfour_games_loser_idx ON public.connectfour_games USING btree (loser_id);
CREATE INDEX connectfour_games_player_red_idx ON public.connectfour_games USING btree (player_red_id);
CREATE INDEX connectfour_games_player_yellow_idx ON public.connectfour_games USING btree (player_yellow_id);
CREATE INDEX connectfour_games_winner_idx ON public.connectfour_games USING btree (winner_id);

CREATE TABLE public.download_stats (
    user_id bigint NOT NULL,
    site text NOT NULL,
    downloads bigint DEFAULT 0 NOT NULL,
    first_downloaded_at timestamp with time zone DEFAULT now() NOT NULL,
    last_downloaded_at timestamp with time zone DEFAULT now() NOT NULL,
    auto_download boolean DEFAULT false NOT NULL,
    CONSTRAINT download_stats_downloads_check CHECK ((downloads >= 0)),
    CONSTRAINT download_stats_site_check CHECK ((site <> ''::text)),
    CONSTRAINT download_stats_pkey PRIMARY KEY (user_id, site, auto_download)
);
CREATE INDEX download_stats_site_idx ON public.download_stats USING btree (site, downloads DESC);

CREATE TABLE public.emoji_stats (
    author_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    emoji_id text NOT NULL,
    guild_id bigint NOT NULL,
    unicode boolean DEFAULT false NOT NULL
);
CREATE INDEX emoji_stats_author_emoji_idx ON public.emoji_stats USING btree (author_id, emoji_id, unicode);
CREATE INDEX emoji_stats_created_at_idx ON public.emoji_stats USING btree (created_at);
CREATE INDEX emoji_stats_guild_emoji_idx ON public.emoji_stats USING btree (guild_id, emoji_id, unicode);

CREATE TABLE public.fishing_accounts (
    user_id bigint NOT NULL,
    coins bigint DEFAULT 20 NOT NULL,
    equipped_rod_key text,
    equipped_rod_rarity_key text,
    equipped_bait_key text,
    total_catches bigint DEFAULT 0 NOT NULL,
    CONSTRAINT fishing_accounts_coins_check CHECK ((coins >= 0)),
    CONSTRAINT fishing_accounts_total_catches_check CHECK ((total_catches >= 0)),
    CONSTRAINT fishing_accounts_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.fishing_bait (
    user_id bigint NOT NULL,
    bait_key text NOT NULL,
    quantity bigint DEFAULT 0 NOT NULL,
    CONSTRAINT fishing_bait_quantity_check CHECK ((quantity >= 0)),
    CONSTRAINT fishing_bait_pkey PRIMARY KEY (user_id, bait_key),
    CONSTRAINT fishing_bait_account_fk FOREIGN KEY (user_id) REFERENCES public.fishing_accounts(user_id) ON DELETE CASCADE
);

CREATE TABLE public.fishing_catches (
    user_id bigint NOT NULL,
    creature_key text NOT NULL,
    rarity_key text NOT NULL,
    quantity bigint DEFAULT 1 NOT NULL,
    CONSTRAINT fishing_catches_quantity_check CHECK ((quantity > 0)),
    CONSTRAINT fishing_catches_pkey PRIMARY KEY (user_id, creature_key, rarity_key),
    CONSTRAINT fishing_catches_account_fk FOREIGN KEY (user_id) REFERENCES public.fishing_accounts(user_id) ON DELETE CASCADE
);

CREATE TABLE public.fishing_rods (
    user_id bigint NOT NULL,
    rod_key text NOT NULL,
    rarity_key text NOT NULL,
    quantity bigint DEFAULT 1 NOT NULL,
    CONSTRAINT fishing_rods_quantity_check CHECK ((quantity > 0)),
    CONSTRAINT fishing_rods_pkey PRIMARY KEY (user_id, rod_key, rarity_key),
    CONSTRAINT fishing_rods_account_fk FOREIGN KEY (user_id) REFERENCES public.fishing_accounts(user_id) ON DELETE CASCADE
);

CREATE TABLE public.game_2048_games (
    id bigserial NOT NULL,
    user_id bigint NOT NULL,
    guild_id bigint,
    channel_id bigint,
    score bigint DEFAULT 0 NOT NULL,
    highest_tile bigint DEFAULT 0 NOT NULL,
    move_count integer DEFAULT 0 NOT NULL,
    move_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    timed_out boolean DEFAULT false NOT NULL,
    gave_up boolean DEFAULT false NOT NULL,
    duration_seconds bigint DEFAULT 0 NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT game_2048_games_duration_seconds_check CHECK ((duration_seconds >= 0)),
    CONSTRAINT game_2048_games_highest_tile_check CHECK ((highest_tile >= 0)),
    CONSTRAINT game_2048_games_move_count_check CHECK ((move_count >= 0)),
    CONSTRAINT game_2048_games_score_check CHECK ((score >= 0)),
    CONSTRAINT game_2048_games_pkey PRIMARY KEY (id)
);
CREATE INDEX game_2048_games_user_idx ON public.game_2048_games USING btree (user_id, finished_at DESC);

CREATE TABLE public.game_2048_stats (
    user_id bigint NOT NULL,
    high_score bigint DEFAULT 0 NOT NULL,
    total_playtime_seconds bigint DEFAULT 0 NOT NULL,
    games_completed bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT game_2048_stats_games_completed_check CHECK ((games_completed >= 0)),
    CONSTRAINT game_2048_stats_high_score_check CHECK ((high_score >= 0)),
    CONSTRAINT game_2048_stats_total_playtime_seconds_check CHECK ((total_playtime_seconds >= 0)),
    CONSTRAINT game_2048_stats_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.lastletter_stats (
    user_id bigint NOT NULL,
    wins bigint DEFAULT 0 NOT NULL,
    losses bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lastletter_stats_losses_check CHECK ((losses >= 0)),
    CONSTRAINT lastletter_stats_wins_check CHECK ((wins >= 0)),
    CONSTRAINT lastletter_stats_pkey PRIMARY KEY (user_id)
);
CREATE INDEX lastletter_stats_leaderboard_idx ON public.lastletter_stats USING btree (wins DESC, losses, updated_at, user_id);

CREATE TABLE public.lightsout_games (
    id bigserial NOT NULL,
    user_id bigint NOT NULL,
    guild_id bigint,
    channel_id bigint NOT NULL,
    move_count integer NOT NULL,
    duration_seconds double precision NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lightsout_games_duration_seconds_check CHECK ((duration_seconds >= (0)::double precision)),
    CONSTRAINT lightsout_games_move_count_check CHECK ((move_count >= 0)),
    CONSTRAINT lightsout_games_pkey PRIMARY KEY (id)
);
CREATE INDEX lightsout_games_fastest_idx ON public.lightsout_games USING btree (duration_seconds, move_count, finished_at);
CREATE INDEX lightsout_games_fewest_moves_idx ON public.lightsout_games USING btree (move_count, duration_seconds, finished_at);
CREATE INDEX lightsout_games_user_idx ON public.lightsout_games USING btree (user_id, finished_at DESC);

CREATE TABLE public.lucky_roll_stats (
    user_id bigint NOT NULL,
    wins bigint DEFAULT 0 NOT NULL,
    losses bigint DEFAULT 0 NOT NULL,
    coins_earned bigint DEFAULT 0 NOT NULL,
    coins_lost bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lucky_roll_stats_coins_earned_check CHECK ((coins_earned >= 0)),
    CONSTRAINT lucky_roll_stats_coins_lost_check CHECK ((coins_lost >= 0)),
    CONSTRAINT lucky_roll_stats_losses_check CHECK ((losses >= 0)),
    CONSTRAINT lucky_roll_stats_wins_check CHECK ((wins >= 0)),
    CONSTRAINT lucky_roll_stats_pkey PRIMARY KEY (user_id)
);
CREATE INDEX lucky_roll_stats_leaderboard_idx ON public.lucky_roll_stats USING btree (wins DESC, coins_earned DESC, updated_at, user_id);

CREATE TABLE public.minigame_stats (
    game text NOT NULL,
    user_id bigint NOT NULL,
    difficulty text NOT NULL,
    wins bigint DEFAULT 0 NOT NULL,
    fails bigint DEFAULT 0 NOT NULL,
    CONSTRAINT minigame_stats_fails_check CHECK ((fails >= 0)),
    CONSTRAINT minigame_stats_wins_check CHECK ((wins >= 0)),
    CONSTRAINT minigame_stats_pkey PRIMARY KEY (game, user_id, difficulty)
);
CREATE INDEX minigame_stats_game_idx ON public.minigame_stats USING btree (game, difficulty, wins DESC);

CREATE TABLE public.pokemon_guesses (
    pokemon_name text NOT NULL,
    author_id bigint NOT NULL,
    correct bigint DEFAULT 0,
    incorrect bigint DEFAULT 0,
    CONSTRAINT pokemon_guesses_pkey PRIMARY KEY (pokemon_name, author_id)
);

CREATE TABLE public.pokemon_solves (
    id serial NOT NULL,
    user_id bigint NOT NULL,
    pokemon_name text NOT NULL,
    method text NOT NULL,
    guild_id bigint,
    created_at timestamp with time zone DEFAULT now()
);
CREATE INDEX pokemon_solves_user_created_idx ON public.pokemon_solves USING btree (user_id, created_at DESC);

CREATE TABLE public.sea_animal_race_stats (
    user_id bigint NOT NULL,
    wins bigint DEFAULT 0 NOT NULL,
    losses bigint DEFAULT 0 NOT NULL,
    faints bigint DEFAULT 0 NOT NULL,
    coins_earned bigint DEFAULT 0 NOT NULL,
    coins_lost bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sea_animal_race_stats_coins_earned_check CHECK ((coins_earned >= 0)),
    CONSTRAINT sea_animal_race_stats_coins_lost_check CHECK ((coins_lost >= 0)),
    CONSTRAINT sea_animal_race_stats_faints_check CHECK ((faints >= 0)),
    CONSTRAINT sea_animal_race_stats_losses_check CHECK ((losses >= 0)),
    CONSTRAINT sea_animal_race_stats_wins_check CHECK ((wins >= 0)),
    CONSTRAINT sea_animal_race_stats_pkey PRIMARY KEY (user_id)
);
CREATE INDEX sea_animal_race_stats_leaderboard_idx ON public.sea_animal_race_stats USING btree (wins DESC, coins_earned DESC, updated_at, user_id);

CREATE TABLE public.sold_fish (
    user_id bigint NOT NULL,
    bass bigint,
    commandtuna bigint,
    salmon bigint,
    caught_fish bigint,
    carp bigint,
    trout bigint,
    sardine bigint,
    blue_tang bigint,
    pike bigint,
    mackerel bigint,
    red_snapper bigint,
    shark bigint,
    hammerhead_shark bigint,
    great_white_shark bigint,
    leviathan bigint,
    kraken bigint,
    CONSTRAINT sold_fish_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.streak_game_stats (
    game text NOT NULL,
    user_id bigint NOT NULL,
    highest_streak bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    highest_streak_payout bigint DEFAULT 0 NOT NULL,
    highest_payout bigint DEFAULT 0 NOT NULL,
    highest_payout_streak bigint DEFAULT 0 NOT NULL,
    CONSTRAINT streak_game_stats_game_check CHECK ((game = ANY (ARRAY['higher_or_lower'::text, 'heads_or_tails'::text, 'rock_paper_scissors'::text]))),
    CONSTRAINT streak_game_stats_highest_payout_check CHECK ((highest_payout >= 0)),
    CONSTRAINT streak_game_stats_highest_payout_streak_check CHECK ((highest_payout_streak >= 0)),
    CONSTRAINT streak_game_stats_highest_streak_check CHECK ((highest_streak >= 0)),
    CONSTRAINT streak_game_stats_highest_streak_payout_check CHECK ((highest_streak_payout >= 0)),
    CONSTRAINT streak_game_stats_pkey PRIMARY KEY (game, user_id)
);
CREATE INDEX streak_game_stats_leaderboard_idx ON public.streak_game_stats USING btree (game, highest_streak DESC, updated_at, user_id);
CREATE INDEX streak_game_stats_payout_leaderboard_idx ON public.streak_game_stats USING btree (game, highest_payout DESC, highest_payout_streak DESC, user_id);

CREATE TABLE public.tictactoe_games (
    id bigserial NOT NULL,
    guild_id bigint,
    channel_id bigint NOT NULL,
    player_x_id bigint NOT NULL,
    player_o_id bigint NOT NULL,
    winner_id bigint,
    loser_id bigint,
    against_bot boolean DEFAULT false NOT NULL,
    bot_difficulty text,
    started_by_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tictactoe_bot_difficulty_check CHECK (((against_bot AND (bot_difficulty = ANY (ARRAY['easy'::text, 'normal'::text, 'hard'::text]))) OR ((NOT against_bot) AND (bot_difficulty IS NULL)))),
    CONSTRAINT tictactoe_loser_player_check CHECK (((loser_id IS NULL) OR ((loser_id = player_x_id) OR (loser_id = player_o_id)))),
    CONSTRAINT tictactoe_winner_loser_check CHECK (((winner_id IS NULL) OR (loser_id IS NULL) OR (winner_id <> loser_id))),
    CONSTRAINT tictactoe_winner_player_check CHECK (((winner_id IS NULL) OR ((winner_id = player_x_id) OR (winner_id = player_o_id)))),
    CONSTRAINT tictactoe_games_pkey PRIMARY KEY (id)
);
CREATE INDEX tictactoe_games_loser_idx ON public.tictactoe_games USING btree (loser_id);
CREATE INDEX tictactoe_games_player_o_idx ON public.tictactoe_games USING btree (player_o_id);
CREATE INDEX tictactoe_games_player_x_idx ON public.tictactoe_games USING btree (player_x_id);
CREATE INDEX tictactoe_games_winner_idx ON public.tictactoe_games USING btree (winner_id);

CREATE TABLE public.user_fishing (
    user_id bigint NOT NULL,
    rod_level integer,
    fish_caught bigint,
    coins bigint,
    CONSTRAINT user_fishing_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.wordbomb_custom_words (
    word text NOT NULL,
    added_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wordbomb_custom_words_alpha CHECK ((word ~ '^[[:alpha:]]{2,}$'::text)),
    CONSTRAINT wordbomb_custom_words_pkey PRIMARY KEY (word)
);
CREATE INDEX wordbomb_custom_words_added_by_idx ON public.wordbomb_custom_words USING btree (added_by, created_at DESC);

CREATE TABLE public.wordbomb_stats (
    user_id bigint NOT NULL,
    wins bigint DEFAULT 0 NOT NULL,
    losses bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wordbomb_stats_losses_check CHECK ((losses >= 0)),
    CONSTRAINT wordbomb_stats_wins_check CHECK ((wins >= 0)),
    CONSTRAINT wordbomb_stats_pkey PRIMARY KEY (user_id)
);
CREATE INDEX wordbomb_stats_leaderboard_idx ON public.wordbomb_stats USING btree (wins DESC, losses, updated_at, user_id);

CREATE TABLE public.wordle_games (
    id bigserial NOT NULL,
    user_id bigint NOT NULL,
    guild_id bigint,
    channel_id bigint NOT NULL,
    answer text NOT NULL,
    solved boolean NOT NULL,
    attempts smallint NOT NULL,
    hard_mode boolean DEFAULT false NOT NULL,
    colourblind_mode boolean DEFAULT false NOT NULL,
    duration_seconds bigint DEFAULT 0 NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wordle_games_attempts_check CHECK (((attempts >= 0) AND (attempts <= 6))),
    CONSTRAINT wordle_games_duration_seconds_check CHECK ((duration_seconds >= 0)),
    CONSTRAINT wordle_games_pkey PRIMARY KEY (id)
);
CREATE INDEX wordle_games_guild_idx ON public.wordle_games USING btree (guild_id, finished_at DESC);
CREATE INDEX wordle_games_user_idx ON public.wordle_games USING btree (user_id, finished_at DESC);

CREATE TABLE public.wordle_stats (
    user_id bigint NOT NULL,
    wins bigint DEFAULT 0 NOT NULL,
    losses bigint DEFAULT 0 NOT NULL,
    total_playtime_seconds bigint DEFAULT 0 NOT NULL,
    attempt_1 bigint DEFAULT 0 NOT NULL,
    attempt_2 bigint DEFAULT 0 NOT NULL,
    attempt_3 bigint DEFAULT 0 NOT NULL,
    attempt_4 bigint DEFAULT 0 NOT NULL,
    attempt_5 bigint DEFAULT 0 NOT NULL,
    attempt_6 bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wordle_stats_attempt_1_check CHECK ((attempt_1 >= 0)),
    CONSTRAINT wordle_stats_attempt_2_check CHECK ((attempt_2 >= 0)),
    CONSTRAINT wordle_stats_attempt_3_check CHECK ((attempt_3 >= 0)),
    CONSTRAINT wordle_stats_attempt_4_check CHECK ((attempt_4 >= 0)),
    CONSTRAINT wordle_stats_attempt_5_check CHECK ((attempt_5 >= 0)),
    CONSTRAINT wordle_stats_attempt_6_check CHECK ((attempt_6 >= 0)),
    CONSTRAINT wordle_stats_losses_check CHECK ((losses >= 0)),
    CONSTRAINT wordle_stats_total_playtime_seconds_check CHECK ((total_playtime_seconds >= 0)),
    CONSTRAINT wordle_stats_wins_check CHECK ((wins >= 0)),
    CONSTRAINT wordle_stats_pkey PRIMARY KEY (user_id)
);

-- Notifications and Mudae

CREATE TABLE public.highlights (
    user_id bigint NOT NULL,
    guild_id bigint NOT NULL,
    word text NOT NULL,
    word_normalized text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT highlights_pkey PRIMARY KEY (user_id, guild_id, word_normalized)
);
CREATE INDEX highlights_guild_idx ON public.highlights USING btree (guild_id);

CREATE TABLE public.mudae_channels (
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    CONSTRAINT mudae_channels_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.mudae_recent_claims (
    id bigserial NOT NULL,
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    message_id bigint NOT NULL,
    character_name text NOT NULL,
    claiming_username text DEFAULT 'Unknown'::text NOT NULL,
    claiming_user_id bigint DEFAULT 1 NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL,
    event_key text DEFAULT ''::text NOT NULL,
    CONSTRAINT mudae_recent_claims_character_name_check CHECK ((length(btrim(character_name)) > 0)),
    CONSTRAINT mudae_recent_claims_pkey PRIMARY KEY (id)
);
CREATE INDEX mudae_recent_claims_claimant_idx ON public.mudae_recent_claims USING btree (guild_id, claiming_user_id);
CREATE UNIQUE INDEX mudae_recent_claims_event_idx ON public.mudae_recent_claims USING btree (guild_id, message_id, event_key);
CREATE INDEX mudae_recent_claims_guild_time_idx ON public.mudae_recent_claims USING btree (guild_id, claimed_at DESC, id DESC);

CREATE TABLE public.mudae_series_bundles (
    id bigserial NOT NULL,
    guild_id bigint NOT NULL,
    source_guild_id bigint,
    source_channel_id bigint,
    source_message_id bigint,
    bundle_name text NOT NULL,
    bundle_key text NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    latest_page integer,
    latest_total_pages integer,
    latest_message_id bigint,
    CONSTRAINT mudae_series_bundles_bundle_key_check CHECK ((length(btrim(bundle_key)) > 0)),
    CONSTRAINT mudae_series_bundles_bundle_name_check CHECK ((length(btrim(bundle_name)) > 0)),
    CONSTRAINT mudae_series_bundles_latest_page_check CHECK (((latest_page IS NULL) OR (latest_page > 0))),
    CONSTRAINT mudae_series_bundles_latest_total_pages_check CHECK (((latest_total_pages IS NULL) OR (latest_total_pages > 0))),
    CONSTRAINT mudae_series_bundles_guild_id_bundle_key_key UNIQUE (guild_id, bundle_key),
    CONSTRAINT mudae_series_bundles_pkey PRIMARY KEY (id)
);
CREATE INDEX mudae_series_bundles_guild_idx ON public.mudae_series_bundles USING btree (guild_id, bundle_key);
CREATE INDEX mudae_series_bundles_source_message_idx ON public.mudae_series_bundles USING btree (source_message_id) WHERE (source_message_id IS NOT NULL);

CREATE TABLE public.mudae_series (
    id bigserial NOT NULL,
    bundle_id bigint NOT NULL,
    series_name text NOT NULL,
    normalized_name text NOT NULL,
    character_count integer,
    source_guild_id bigint,
    source_channel_id bigint,
    source_message_id bigint,
    source_page integer,
    source_entry integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT mudae_series_character_count_check CHECK (((character_count IS NULL) OR (character_count >= 0))),
    CONSTRAINT mudae_series_normalized_name_check CHECK ((length(btrim(normalized_name)) > 0)),
    CONSTRAINT mudae_series_series_name_check CHECK ((length(btrim(series_name)) > 0)),
    CONSTRAINT mudae_series_source_entry_check CHECK (((source_entry IS NULL) OR (source_entry > 0))),
    CONSTRAINT mudae_series_source_page_check CHECK (((source_page IS NULL) OR (source_page > 0))),
    CONSTRAINT mudae_series_bundle_id_normalized_name_key UNIQUE (bundle_id, normalized_name),
    CONSTRAINT mudae_series_pkey PRIMARY KEY (id),
    CONSTRAINT mudae_series_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES public.mudae_series_bundles(id) ON DELETE CASCADE
);
CREATE INDEX mudae_series_bundle_idx ON public.mudae_series USING btree (bundle_id, source_page, source_entry);
CREATE INDEX mudae_series_name_idx ON public.mudae_series USING btree (normalized_name);
CREATE INDEX mudae_series_source_message_idx ON public.mudae_series USING btree (source_message_id) WHERE (source_message_id IS NOT NULL);

CREATE TABLE public.mudae_subs (
    guild_id bigint NOT NULL,
    item text NOT NULL,
    user_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    CONSTRAINT mudae_subs_pkey PRIMARY KEY (guild_id, item)
);

CREATE TABLE public.mudae_timers (
    guild_id bigint NOT NULL,
    item text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT mudae_timers_pkey PRIMARY KEY (guild_id, item)
);

CREATE TABLE public.mudae_wishes (
    guild_id bigint NOT NULL,
    user_id bigint NOT NULL,
    wish_type text NOT NULL,
    wish_value text NOT NULL,
    id bigserial NOT NULL,
    bundle_id bigint,
    bundle_key text,
    bundle_name text,
    bundle_created_at timestamp with time zone,
    source_guild_id bigint,
    source_channel_id bigint,
    source_message_id bigint,
    source_page integer,
    source_entry integer,
    kakera_threshold bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT mudae_wishes_check CHECK (((wish_type <> 'series_kakera'::text) OR (kakera_threshold IS NOT NULL))),
    CONSTRAINT mudae_wishes_kakera_threshold_check CHECK (((kakera_threshold IS NULL) OR (kakera_threshold >= 0))),
    CONSTRAINT mudae_wishes_series_kakera_check CHECK (((wish_type <> 'series_kakera'::text) OR (kakera_threshold IS NOT NULL))),
    CONSTRAINT mudae_wishes_source_entry_check CHECK (((source_entry IS NULL) OR (source_entry > 0))),
    CONSTRAINT mudae_wishes_source_page_check CHECK (((source_page IS NULL) OR (source_page > 0))),
    CONSTRAINT mudae_wishes_wish_type_check CHECK ((wish_type = ANY (ARRAY['character'::text, 'series'::text, 'series_kakera'::text, 'kakera'::text]))),
    CONSTRAINT mudae_wishes_wish_value_check CHECK ((length(TRIM(BOTH FROM wish_value)) > 0)),
    CONSTRAINT mudae_wishes_id_key UNIQUE (id),
    CONSTRAINT mudae_wishes_pkey PRIMARY KEY (guild_id, user_id, wish_type, wish_value)
);
CREATE INDEX mudae_wishes_bundle_idx ON public.mudae_wishes USING btree (guild_id, user_id, bundle_key) WHERE (bundle_key IS NOT NULL);
CREATE INDEX mudae_wishes_guild_idx ON public.mudae_wishes USING btree (guild_id);
CREATE UNIQUE INDEX mudae_wishes_id_idx ON public.mudae_wishes USING btree (id);
CREATE INDEX mudae_wishes_source_message_idx ON public.mudae_wishes USING btree (source_message_id) WHERE (source_message_id IS NOT NULL);
CREATE INDEX mudae_wishes_user_idx ON public.mudae_wishes USING btree (user_id);

CREATE TABLE public.notify_anime_follows (
    id bigserial NOT NULL,
    guild_id bigint,
    user_id bigint,
    anilist_id bigint NOT NULL,
    title text NOT NULL,
    site_url text,
    banner_url text,
    official_site_url text,
    crunchyroll_url text,
    announce_channel_id bigint,
    mention_role_id bigint,
    mention_everyone boolean DEFAULT false NOT NULL,
    release_at timestamp with time zone,
    next_airing_at timestamp with time zone,
    next_episode integer,
    last_checked_at timestamp with time zone,
    last_notified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notify_anime_episode_check CHECK (((next_episode IS NULL) OR (next_episode > 0))),
    CONSTRAINT notify_anime_id_check CHECK ((anilist_id > 0)),
    CONSTRAINT notify_anime_mention_check CHECK ((NOT ((mention_role_id IS NOT NULL) AND mention_everyone))),
    CONSTRAINT notify_anime_owner_check CHECK (((guild_id IS NOT NULL) <> (user_id IS NOT NULL))),
    CONSTRAINT notify_anime_title_check CHECK (((length(btrim(title)) >= 1) AND (length(btrim(title)) <= 500))),
    CONSTRAINT notify_anime_follows_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE public.notify_anime_follows IS 'Notify follows: maximum 20 distinct AniList entries per guild or DM scope; one row per followed anime and scope.';
CREATE INDEX notify_anime_airing_idx ON public.notify_anime_follows USING btree (next_airing_at) WHERE (next_airing_at IS NOT NULL);
CREATE INDEX notify_anime_guild_idx ON public.notify_anime_follows USING btree (guild_id) WHERE (guild_id IS NOT NULL);
CREATE UNIQUE INDEX notify_anime_guild_key_idx ON public.notify_anime_follows USING btree (guild_id, anilist_id) WHERE (guild_id IS NOT NULL);
CREATE INDEX notify_anime_user_idx ON public.notify_anime_follows USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE UNIQUE INDEX notify_anime_user_key_idx ON public.notify_anime_follows USING btree (user_id, anilist_id) WHERE (user_id IS NOT NULL);

CREATE TABLE public.notify_twitch_follows (
    id bigserial NOT NULL,
    guild_id bigint,
    user_id bigint,
    channel_name text NOT NULL,
    broadcaster_id text,
    announce_channel_id bigint,
    mention_role_id bigint,
    mention_everyone boolean DEFAULT false NOT NULL,
    last_stream_id text,
    last_live_at timestamp with time zone,
    last_offline_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notify_twitch_channel_name_check CHECK (((length(btrim(channel_name)) >= 1) AND (length(btrim(channel_name)) <= 25))),
    CONSTRAINT notify_twitch_mention_check CHECK ((NOT ((mention_role_id IS NOT NULL) AND mention_everyone))),
    CONSTRAINT notify_twitch_owner_check CHECK (((guild_id IS NOT NULL) <> (user_id IS NOT NULL))),
    CONSTRAINT notify_twitch_follows_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE public.notify_twitch_follows IS 'Notify follows: maximum 10 distinct Twitch channels per guild or DM scope; one row per followed channel and scope.';
CREATE INDEX notify_twitch_broadcaster_idx ON public.notify_twitch_follows USING btree (broadcaster_id);
CREATE INDEX notify_twitch_guild_idx ON public.notify_twitch_follows USING btree (guild_id) WHERE (guild_id IS NOT NULL);
CREATE UNIQUE INDEX notify_twitch_guild_key_idx ON public.notify_twitch_follows USING btree (guild_id, lower(btrim(channel_name))) WHERE (guild_id IS NOT NULL);
CREATE INDEX notify_twitch_user_idx ON public.notify_twitch_follows USING btree (user_id) WHERE (user_id IS NOT NULL);
CREATE UNIQUE INDEX notify_twitch_user_key_idx ON public.notify_twitch_follows USING btree (user_id, lower(btrim(channel_name))) WHERE (user_id IS NOT NULL);

CREATE TABLE public.reminders (
    id serial NOT NULL,
    expires timestamp with time zone,
    created timestamp with time zone DEFAULT now(),
    event text,
    extra jsonb DEFAULT '{}'::jsonb,
    timezone text DEFAULT 'UTC'::text NOT NULL,
    CONSTRAINT reminders_pkey PRIMARY KEY (id)
);
CREATE INDEX reminders_expires_idx ON public.reminders USING btree (expires);
CREATE INDEX reminders_user_expires_idx ON public.reminders USING btree (((extra #>> '{args,0}'::text[])), expires) WHERE (event = 'reminder'::text);

CREATE TABLE public.twitch_announcement_deliveries (
    guild_id bigint NOT NULL,
    channel_name text NOT NULL,
    stream_id text NOT NULL,
    stream_payload jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    CONSTRAINT twitch_announcement_deliveries_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'done'::text, 'dead'::text]))),
    CONSTRAINT twitch_announcement_deliveries_pkey PRIMARY KEY (guild_id, channel_name, stream_id)
);

CREATE TABLE public.twitch_eventsub_events (
    message_id text NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    payload jsonb,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    CONSTRAINT twitch_eventsub_events_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'done'::text, 'dead'::text]))),
    CONSTRAINT twitch_eventsub_events_pkey PRIMARY KEY (message_id)
);
CREATE INDEX twitch_eventsub_events_pending_idx ON public.twitch_eventsub_events USING btree (next_attempt_at) WHERE (status = ANY (ARRAY['pending'::text, 'processing'::text]));

CREATE TABLE public.twitch_eventsub_subscriptions (
    broadcaster_id text NOT NULL,
    subscription_id text NOT NULL,
    status text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT twitch_eventsub_subscriptions_pkey PRIMARY KEY (broadcaster_id)
);

CREATE TABLE public.twitch_follows (
    guild_id bigint NOT NULL,
    channel_name text NOT NULL,
    announce_channel_id bigint NOT NULL,
    message_template text,
    broadcaster_id text,
    last_stream_id text,
    CONSTRAINT twitch_follows_pkey PRIMARY KEY (guild_id, channel_name)
);
CREATE INDEX twitch_follows_guild_idx ON public.twitch_follows USING btree (guild_id);

CREATE TABLE public.youtube_announcement_deliveries (
    guild_id bigint NOT NULL,
    youtube_channel_id text NOT NULL,
    item_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    CONSTRAINT youtube_announcement_event_type_check CHECK ((event_type = ANY (ARRAY['video'::text, 'live'::text, 'short'::text, 'community'::text]))),
    CONSTRAINT youtube_announcement_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'done'::text, 'dead'::text]))),
    CONSTRAINT youtube_announcement_deliveries_pkey PRIMARY KEY (guild_id, youtube_channel_id, item_id, event_type)
);

CREATE TABLE public.youtube_community_state (
    youtube_channel_id text NOT NULL,
    latest_post_id text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT youtube_community_state_pkey PRIMARY KEY (youtube_channel_id)
);

CREATE TABLE public.youtube_events (
    event_id text NOT NULL,
    youtube_channel_id text NOT NULL,
    video_id text NOT NULL,
    payload jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    CONSTRAINT youtube_events_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'done'::text, 'dead'::text]))),
    CONSTRAINT youtube_events_pkey PRIMARY KEY (event_id)
);

CREATE TABLE public.youtube_follows (
    guild_id bigint NOT NULL,
    youtube_channel_id text NOT NULL,
    channel_name text NOT NULL,
    channel_handle text,
    announce_channel_id bigint NOT NULL,
    message_template text,
    event_types text[] DEFAULT ARRAY['video'::text, 'live'::text, 'short'::text, 'community'::text] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT youtube_follows_event_types_check CHECK (((cardinality(event_types) > 0) AND (event_types <@ ARRAY['video'::text, 'live'::text, 'short'::text, 'community'::text]))),
    CONSTRAINT youtube_follows_pkey PRIMARY KEY (guild_id, youtube_channel_id)
);
CREATE INDEX youtube_follows_channel_idx ON public.youtube_follows USING btree (youtube_channel_id);

CREATE TABLE public.youtube_websub_subscriptions (
    youtube_channel_id text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    lease_expires_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    CONSTRAINT youtube_websub_subscriptions_pkey PRIMARY KEY (youtube_channel_id)
);

-- History and reactions

CREATE TABLE public.avatars (
    id serial NOT NULL,
    user_id bigint NOT NULL,
    avatar_key text NOT NULL,
    created_at timestamp with time zone,
    avatar text,
    CONSTRAINT avatars_pkey PRIMARY KEY (user_id, avatar_key)
);
CREATE INDEX avatars_created_idx ON public.avatars USING btree (created_at);
CREATE INDEX avatars_user_created_idx ON public.avatars USING btree (user_id, created_at DESC);

CREATE TABLE public.command_logs (
    user_id bigint,
    guild_id bigint,
    channel_id bigint,
    message_id bigint,
    command text,
    created_at timestamp with time zone
);
CREATE INDEX command_logs_created_idx ON public.command_logs USING btree (created_at);
CREATE INDEX command_logs_guild_command_created_idx ON public.command_logs USING btree (guild_id, command, created_at DESC);
CREATE INDEX command_logs_guild_created_idx ON public.command_logs USING btree (guild_id, created_at DESC);
CREATE INDEX command_logs_user_created_idx ON public.command_logs USING btree (user_id, created_at DESC);

CREATE TABLE public.corn_reacts (
    id serial NOT NULL,
    receiver_id bigint NOT NULL,
    giver_id bigint NOT NULL,
    guild_id bigint,
    message_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT corn_reacts_pkey PRIMARY KEY (id)
);
CREATE INDEX corn_reacts_giver_idx ON public.corn_reacts USING btree (giver_id);
CREATE UNIQUE INDEX corn_reacts_giver_message_idx ON public.corn_reacts USING btree (giver_id, message_id);
CREATE INDEX corn_reacts_guild_idx ON public.corn_reacts USING btree (guild_id);
CREATE INDEX corn_reacts_receiver_idx ON public.corn_reacts USING btree (receiver_id);

CREATE TABLE public.discrim_logs (
    id serial NOT NULL,
    user_id bigint,
    discrim text,
    created_at timestamp with time zone
);
CREATE INDEX discrim_logs_created_idx ON public.discrim_logs USING btree (created_at);
CREATE INDEX discrim_logs_user_created_idx ON public.discrim_logs USING btree (user_id, created_at DESC);

CREATE TABLE public.display_name_logs (
    id serial NOT NULL,
    user_id bigint,
    display_name text,
    created_at timestamp with time zone
);
CREATE INDEX display_name_logs_created_idx ON public.display_name_logs USING btree (created_at);
CREATE INDEX display_name_logs_user_created_idx ON public.display_name_logs USING btree (user_id, created_at DESC);

CREATE TABLE public.guild_avatars (
    id serial NOT NULL,
    member_id bigint NOT NULL,
    guild_id bigint NOT NULL,
    avatar_key text NOT NULL,
    created_at timestamp with time zone,
    avatar text,
    CONSTRAINT guild_avatars_pkey PRIMARY KEY (member_id, avatar_key, guild_id)
);
CREATE INDEX guild_avatars_created_idx ON public.guild_avatars USING btree (created_at);

CREATE TABLE public.guild_icons (
    id serial NOT NULL,
    guild_id bigint NOT NULL,
    icon_key text NOT NULL,
    created_at timestamp with time zone,
    icon text,
    CONSTRAINT guild_icons_pkey PRIMARY KEY (icon_key, guild_id)
);
CREATE INDEX guild_icons_created_idx ON public.guild_icons USING btree (created_at);

CREATE TABLE public.guild_join_logs (
    guild_id bigint,
    owner_id bigint,
    "time" timestamp with time zone,
    added_by bigint
);

CREATE TABLE public.guild_name_logs (
    id serial NOT NULL,
    guild_id bigint,
    name text,
    created_at timestamp with time zone
);
CREATE INDEX guild_name_logs_created_idx ON public.guild_name_logs USING btree (created_at);

CREATE TABLE public.member_join_logs (
    id serial NOT NULL,
    member_id bigint,
    guild_id bigint,
    "time" timestamp with time zone
);
CREATE INDEX member_join_logs_member_guild_time_idx ON public.member_join_logs USING btree (member_id, guild_id, "time" DESC);
CREATE INDEX member_join_logs_time_idx ON public.member_join_logs USING btree ("time");

CREATE TABLE public.nickname_logs (
    id serial NOT NULL,
    user_id bigint,
    guild_id bigint,
    nickname text,
    created_at timestamp with time zone
);
CREATE INDEX nickname_logs_created_idx ON public.nickname_logs USING btree (created_at);
CREATE INDEX nickname_logs_user_guild_created_idx ON public.nickname_logs USING btree (user_id, guild_id, created_at DESC);

CREATE TABLE public.reaction_logs (
    id bigserial NOT NULL,
    giver_id bigint NOT NULL,
    receiver_id bigint NOT NULL,
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    message_id bigint NOT NULL,
    emoji_name text NOT NULL,
    emoji_id bigint,
    unicode boolean DEFAULT false NOT NULL,
    user_created_at timestamp with time zone,
    guild_created_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT reaction_logs_pkey PRIMARY KEY (id)
);
CREATE INDEX reaction_logs_giver_idx ON public.reaction_logs USING btree (giver_id);
CREATE INDEX reaction_logs_guild_emoji_idx ON public.reaction_logs USING btree (guild_id, emoji_name, emoji_id, unicode);
CREATE INDEX reaction_logs_guild_giver_idx ON public.reaction_logs USING btree (guild_id, giver_id);
CREATE INDEX reaction_logs_guild_receiver_idx ON public.reaction_logs USING btree (guild_id, receiver_id);
CREATE UNIQUE INDEX reaction_logs_message_giver_emoji_idx ON public.reaction_logs USING btree (message_id, giver_id, emoji_name, COALESCE(emoji_id, (0)::bigint), unicode);
CREATE INDEX reaction_logs_receiver_idx ON public.reaction_logs USING btree (receiver_id);

CREATE TABLE public.reaction_tracking (
    user_id bigint NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT reaction_tracking_pkey PRIMARY KEY (user_id)
);

CREATE TABLE public.stag_logs (
    id serial NOT NULL,
    user_id bigint NOT NULL,
    tag text,
    guild_id bigint,
    guild_created_at timestamp with time zone,
    badge_url text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE INDEX stag_logs_guild_idx ON public.stag_logs USING btree (guild_id) WHERE (guild_id IS NOT NULL);
CREATE INDEX stag_logs_user_created_idx ON public.stag_logs USING btree (user_id, created_at DESC);

CREATE TABLE public.user_status_history (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    guild_id bigint NOT NULL,
    status text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_status_history_interval_check CHECK (((ended_at IS NULL) OR (ended_at >= started_at))),
    CONSTRAINT user_status_history_status_check CHECK ((status = ANY (ARRAY['offline'::text, 'idle'::text, 'online'::text, 'dnd'::text]))),
    CONSTRAINT user_status_history_pkey PRIMARY KEY (id)
);
ALTER TABLE public.user_status_history ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.user_status_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE UNIQUE INDEX user_status_history_active_idx ON public.user_status_history USING btree (user_id, guild_id) WHERE (ended_at IS NULL);
CREATE INDEX user_status_history_retention_idx ON public.user_status_history USING btree (ended_at) WHERE (ended_at IS NOT NULL);
CREATE INDEX user_status_history_user_guild_started_idx ON public.user_status_history USING btree (user_id, guild_id, started_at DESC);

CREATE TABLE public.user_statuses (
    user_id bigint NOT NULL,
    guild_id bigint NOT NULL,
    status text NOT NULL,
    last_seen timestamp with time zone DEFAULT now(),
    CONSTRAINT user_statuses_pkey PRIMARY KEY (user_id, guild_id, status)
);

CREATE TABLE public.user_statuses_legacy_backup (
    user_id bigint CONSTRAINT user_statuses_user_id_not_null NOT NULL,
    guild_id bigint CONSTRAINT user_statuses_guild_id_not_null NOT NULL,
    status text CONSTRAINT user_statuses_status_not_null NOT NULL,
    last_seen timestamp with time zone DEFAULT now(),
    CONSTRAINT user_statuses_legacy_backup_pkey PRIMARY KEY (user_id, guild_id, status)
);

CREATE TABLE public.username_logs (
    id serial NOT NULL,
    user_id bigint,
    username text,
    created_at timestamp with time zone
);
CREATE INDEX username_logs_created_idx ON public.username_logs USING btree (created_at);
CREATE INDEX username_logs_user_created_idx ON public.username_logs USING btree (user_id, created_at DESC);

-- Media and libraries

CREATE TABLE public.download_events (
    id bigserial NOT NULL,
    user_id bigint NOT NULL,
    guild_id bigint NOT NULL,
    channel_id bigint,
    site text NOT NULL,
    auto_download boolean DEFAULT false NOT NULL,
    downloaded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT download_events_site_check CHECK ((site <> ''::text)),
    CONSTRAINT download_events_pkey PRIMARY KEY (id)
);
CREATE INDEX download_events_guild_downloaded_idx ON public.download_events USING btree (guild_id, downloaded_at DESC);
CREATE INDEX download_events_user_downloaded_idx ON public.download_events USING btree (user_id, downloaded_at DESC);

CREATE TABLE public.post_library_deleted_ids (
    library_id bigint NOT NULL,
    deleted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT post_library_deleted_ids_pkey PRIMARY KEY (library_id)
);

CREATE TABLE public.post_uploads (
    id bigserial NOT NULL,
    source_url text,
    filename text NOT NULL,
    review_message_id bigint,
    uploader_id bigint NOT NULL,
    source_guild_id bigint,
    source_channel_id bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    approved_by bigint,
    denied_by bigint,
    denial_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone,
    library_id bigint,
    CONSTRAINT post_uploads_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text]))),
    CONSTRAINT post_uploads_pkey PRIMARY KEY (id)
);
ALTER TABLE public.post_uploads
    ADD CONSTRAINT post_uploads_approved_source_discord CHECK (((status <> 'approved'::text) OR (source_url ~ '^https://(cdn\.discordapp\.com|media\.discordapp\.net)/'::text))) NOT VALID;
ALTER TABLE public.post_uploads
    ADD CONSTRAINT post_uploads_approved_source_required CHECK (((status <> 'approved'::text) OR (source_url IS NOT NULL))) NOT VALID;
CREATE INDEX post_uploads_approved_uploader_idx ON public.post_uploads USING btree (status, uploader_id, id);
CREATE UNIQUE INDEX post_uploads_library_id_idx ON public.post_uploads USING btree (library_id) WHERE (library_id IS NOT NULL);
CREATE INDEX post_uploads_status_idx ON public.post_uploads USING btree (status, id);
CREATE INDEX post_uploads_uploader_idx ON public.post_uploads USING btree (uploader_id, status);

CREATE TABLE public.post_aliases (
    post_id bigint NOT NULL,
    user_id bigint NOT NULL,
    alias text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT post_aliases_alias_length CHECK (((char_length(btrim(alias)) >= 1) AND (char_length(btrim(alias)) <= 100))),
    CONSTRAINT post_aliases_pkey PRIMARY KEY (user_id, post_id),
    CONSTRAINT post_aliases_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post_uploads(id) ON DELETE CASCADE
);
CREATE INDEX post_aliases_post_idx ON public.post_aliases USING btree (post_id);
CREATE UNIQUE INDEX post_aliases_user_alias_idx ON public.post_aliases USING btree (user_id, lower(btrim(alias)));

CREATE TABLE public.post_library_hides (
    user_id bigint NOT NULL,
    post_id bigint NOT NULL,
    hidden_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT post_library_hides_pkey PRIMARY KEY (user_id, post_id),
    CONSTRAINT post_library_hides_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.post_uploads(id) ON DELETE CASCADE
);
CREATE INDEX post_library_hides_post_idx ON public.post_library_hides USING btree (post_id);

CREATE TABLE public.video_library_deleted_ids (
    library_id bigint NOT NULL,
    deleted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_library_deleted_ids_pkey PRIMARY KEY (library_id)
);

CREATE TABLE public.video_uploads (
    id bigserial NOT NULL,
    source_url text,
    filename text NOT NULL,
    review_message_id bigint,
    uploader_id bigint NOT NULL,
    source_guild_id bigint,
    source_channel_id bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    approved_by bigint,
    denied_by bigint,
    denial_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone,
    library_id bigint,
    CONSTRAINT video_uploads_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text]))),
    CONSTRAINT video_uploads_pkey PRIMARY KEY (id)
);
ALTER TABLE public.video_uploads
    ADD CONSTRAINT video_uploads_approved_source_discord CHECK (((status <> 'approved'::text) OR (source_url ~ '^https://(cdn\.discordapp\.com|media\.discordapp\.net)/'::text))) NOT VALID;
ALTER TABLE public.video_uploads
    ADD CONSTRAINT video_uploads_approved_source_required CHECK (((status <> 'approved'::text) OR (source_url IS NOT NULL))) NOT VALID;
CREATE INDEX video_uploads_approved_uploader_idx ON public.video_uploads USING btree (status, uploader_id, id);
CREATE UNIQUE INDEX video_uploads_library_id_idx ON public.video_uploads USING btree (library_id) WHERE (library_id IS NOT NULL);
CREATE INDEX video_uploads_status_idx ON public.video_uploads USING btree (status, id);
CREATE INDEX video_uploads_uploader_idx ON public.video_uploads USING btree (uploader_id, status);

CREATE TABLE public.video_aliases (
    video_id bigint NOT NULL,
    user_id bigint NOT NULL,
    alias text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_aliases_alias_length CHECK (((char_length(btrim(alias)) >= 1) AND (char_length(btrim(alias)) <= 100))),
    CONSTRAINT video_aliases_pkey PRIMARY KEY (user_id, video_id),
    CONSTRAINT video_aliases_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.video_uploads(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX video_aliases_user_alias_idx ON public.video_aliases USING btree (user_id, lower(btrim(alias)));
CREATE INDEX video_aliases_video_idx ON public.video_aliases USING btree (video_id);

CREATE TABLE public.video_library_hides (
    user_id bigint NOT NULL,
    video_id bigint NOT NULL,
    hidden_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_library_hides_pkey PRIMARY KEY (user_id, video_id),
    CONSTRAINT video_library_hides_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.video_uploads(id) ON DELETE CASCADE
);
CREATE INDEX video_library_hides_video_idx ON public.video_library_hides USING btree (video_id);

-- Server configuration and moderation

CREATE TABLE public.bot_restart_state (
    singleton boolean DEFAULT true NOT NULL,
    channel_id bigint NOT NULL,
    message_id bigint NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT bot_restart_state_singleton_check CHECK (singleton),
    CONSTRAINT bot_restart_state_pkey PRIMARY KEY (singleton)
);

CREATE TABLE public.channel_locks (
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    had_overwrite boolean NOT NULL,
    allow_bits bigint DEFAULT 0 NOT NULL,
    deny_bits bigint DEFAULT 0 NOT NULL,
    locked_by bigint NOT NULL,
    locked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT channel_locks_pkey PRIMARY KEY (guild_id, channel_id)
);
CREATE INDEX channel_locks_guild_idx ON public.channel_locks USING btree (guild_id);

CREATE TABLE public.command_config (
    id serial NOT NULL,
    guild_id bigint,
    channel_id bigint,
    name text,
    whitelist boolean,
    CONSTRAINT command_config_pkey PRIMARY KEY (id)
);
CREATE INDEX command_config_guild_id_idx ON public.command_config USING btree (guild_id);

CREATE TABLE public.command_disables (
    guild_id bigint NOT NULL,
    command text NOT NULL,
    channel_id bigint DEFAULT 0 NOT NULL,
    CONSTRAINT command_disables_pkey PRIMARY KEY (guild_id, command, channel_id)
);
CREATE INDEX command_disables_guild_channel_idx ON public.command_disables USING btree (guild_id, channel_id);

CREATE TABLE public.custom_role_assignments (
    guild_id bigint NOT NULL,
    role_id bigint NOT NULL,
    user_id bigint NOT NULL,
    assigned_by bigint NOT NULL,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT custom_role_assignments_pkey PRIMARY KEY (guild_id, role_id, user_id)
);
CREATE INDEX custom_role_assignments_role_idx ON public.custom_role_assignments USING btree (guild_id, role_id);
CREATE INDEX custom_role_assignments_user_idx ON public.custom_role_assignments USING btree (guild_id, user_id);

CREATE TABLE public.custom_roles (
    guild_id bigint NOT NULL,
    role_id bigint NOT NULL,
    created_by bigint NOT NULL,
    booster_only boolean DEFAULT false NOT NULL,
    emoji text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT custom_roles_pkey PRIMARY KEY (guild_id, role_id)
);
CREATE INDEX custom_roles_guild_idx ON public.custom_roles USING btree (guild_id);

CREATE TABLE public.global_command_disables (
    target text NOT NULL,
    target_type text NOT NULL,
    disabled_by bigint NOT NULL,
    disabled_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT global_command_disables_target_type_check CHECK ((target_type = ANY (ARRAY['command'::text, 'cog'::text]))),
    CONSTRAINT global_command_disables_pkey PRIMARY KEY (target)
);
CREATE INDEX global_command_disables_type_idx ON public.global_command_disables USING btree (target_type);

CREATE TABLE public.guild_auto_reaction_channels (
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_auto_reaction_channels_pkey PRIMARY KEY (guild_id, channel_id)
);
CREATE INDEX guild_auto_reaction_channels_channel_idx ON public.guild_auto_reaction_channels USING btree (channel_id, guild_id);

CREATE TABLE public.guild_boards (
    guild_id bigint NOT NULL,
    board_type text NOT NULL,
    channel_id bigint NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    threshold integer DEFAULT 3 NOT NULL,
    allow_nsfw boolean DEFAULT true NOT NULL,
    emoji_name text NOT NULL,
    emoji_id bigint,
    emoji_animated boolean DEFAULT false NOT NULL,
    emoji_set_by bigint,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_boards_board_type_check CHECK ((board_type = ANY (ARRAY['starboard'::text, 'clownboard'::text]))),
    CONSTRAINT guild_boards_check CHECK (((emoji_id IS NOT NULL) OR (NOT emoji_animated))),
    CONSTRAINT guild_boards_emoji_name_check CHECK ((length(emoji_name) > 0)),
    CONSTRAINT guild_boards_threshold_check CHECK ((threshold > 0)),
    CONSTRAINT guild_boards_pkey PRIMARY KEY (guild_id, board_type)
);
CREATE INDEX guild_boards_channel_idx ON public.guild_boards USING btree (channel_id, guild_id);
CREATE UNIQUE INDEX guild_boards_custom_emoji_unique_idx ON public.guild_boards USING btree (guild_id, emoji_id) WHERE (emoji_id IS NOT NULL);
CREATE UNIQUE INDEX guild_boards_unicode_emoji_unique_idx ON public.guild_boards USING btree (guild_id, emoji_name) WHERE (emoji_id IS NULL);

CREATE TABLE public.guild_board_blocks (
    guild_id bigint NOT NULL,
    board_type text NOT NULL,
    target_type text NOT NULL,
    target_id bigint NOT NULL,
    blocked_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_board_blocks_target_type_check CHECK ((target_type = ANY (ARRAY['user'::text, 'channel'::text]))),
    CONSTRAINT guild_board_blocks_pkey PRIMARY KEY (guild_id, board_type, target_type, target_id),
    CONSTRAINT guild_board_blocks_guild_id_board_type_fkey FOREIGN KEY (guild_id, board_type) REFERENCES public.guild_boards(guild_id, board_type) ON DELETE CASCADE
);
CREATE INDEX guild_board_blocks_target_idx ON public.guild_board_blocks USING btree (target_type, target_id, guild_id);

CREATE TABLE public.guild_board_entries (
    guild_id bigint NOT NULL,
    board_type text NOT NULL,
    source_channel_id bigint NOT NULL,
    source_message_id bigint NOT NULL,
    board_channel_id bigint NOT NULL,
    board_message_id bigint NOT NULL,
    reaction_count integer DEFAULT 0 NOT NULL,
    source_author_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_board_entries_reaction_count_check CHECK ((reaction_count >= 0)),
    CONSTRAINT guild_board_entries_pkey PRIMARY KEY (guild_id, board_type, source_message_id),
    CONSTRAINT guild_board_entries_guild_id_board_type_fkey FOREIGN KEY (guild_id, board_type) REFERENCES public.guild_boards(guild_id, board_type) ON DELETE CASCADE
);
CREATE UNIQUE INDEX guild_board_entries_message_unique_idx ON public.guild_board_entries USING btree (board_message_id);
CREATE INDEX guild_board_entries_source_channel_idx ON public.guild_board_entries USING btree (guild_id, source_channel_id, source_message_id);

CREATE TABLE public.guild_hourly_posts (
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    images boolean DEFAULT true NOT NULL,
    gifs boolean DEFAULT true NOT NULL,
    videos boolean DEFAULT true NOT NULL,
    interval_minutes integer DEFAULT 60 NOT NULL,
    next_post_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_hourly_posts_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.guild_log_channels (
    guild_id bigint NOT NULL,
    event text NOT NULL,
    channel_id bigint NOT NULL,
    webhook_url text,
    CONSTRAINT guild_log_channels_pkey PRIMARY KEY (guild_id, event)
);
CREATE INDEX guild_log_channels_guild_idx ON public.guild_log_channels USING btree (guild_id);

CREATE TABLE public.guild_prefixes (
    guild_id bigint NOT NULL,
    prefix text NOT NULL,
    author_id bigint,
    "time" timestamp with time zone,
    CONSTRAINT guild_prefixes_pkey PRIMARY KEY (guild_id, prefix)
);

CREATE TABLE public.guild_protection (
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    protection_role_id bigint,
    enabled boolean DEFAULT true NOT NULL,
    response_mode text DEFAULT 'both'::text NOT NULL,
    allowed_vanity_code text,
    configured_by bigint,
    updated_by bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_protection_response_mode_check CHECK ((response_mode = ANY (ARRAY['warn'::text, 'lock'::text, 'both'::text]))),
    CONSTRAINT guild_protection_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.guild_protection_incidents (
    id bigserial NOT NULL,
    guild_id bigint NOT NULL,
    actor_id bigint NOT NULL,
    target_id bigint,
    trigger text NOT NULL,
    audit_entry_id bigint,
    response_mode text NOT NULL,
    contained boolean DEFAULT false NOT NULL,
    details text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_protection_incidents_response_mode_check CHECK ((response_mode = ANY (ARRAY['warn'::text, 'lock'::text, 'both'::text]))),
    CONSTRAINT guild_protection_incidents_pkey PRIMARY KEY (id)
);
CREATE INDEX guild_protection_incidents_actor_created_idx ON public.guild_protection_incidents USING btree (actor_id, created_at DESC);
CREATE UNIQUE INDEX guild_protection_incidents_audit_idx ON public.guild_protection_incidents USING btree (guild_id, audit_entry_id, trigger) WHERE (audit_entry_id IS NOT NULL);
CREATE INDEX guild_protection_incidents_guild_created_idx ON public.guild_protection_incidents USING btree (guild_id, created_at DESC);

CREATE TABLE public.guild_protection_locks (
    guild_id bigint NOT NULL,
    user_id bigint NOT NULL,
    role_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    trigger text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_protection_locks_pkey PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX guild_protection_locks_user_idx ON public.guild_protection_locks USING btree (user_id);

CREATE TABLE public.guild_protection_triggers (
    guild_id bigint NOT NULL,
    trigger text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    threshold integer NOT NULL,
    window_seconds integer NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT guild_protection_triggers_threshold_check CHECK ((threshold > 0)),
    CONSTRAINT guild_protection_triggers_window_seconds_check CHECK ((window_seconds > 0)),
    CONSTRAINT guild_protection_triggers_pkey PRIMARY KEY (guild_id, trigger),
    CONSTRAINT guild_protection_triggers_guild_id_fkey FOREIGN KEY (guild_id) REFERENCES public.guild_protection(guild_id) ON DELETE CASCADE
);

CREATE TABLE public.honeypot_channels (
    guild_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    message_template text DEFAULT 'This channel was made to catch people who spam in every channel, if you type here there will be no coming back.'::text NOT NULL,
    CONSTRAINT honeypot_channels_pkey PRIMARY KEY (guild_id)
);

CREATE TABLE public.pinboard_pins (
    message_id bigint,
    author_id bigint,
    target_id bigint,
    guild_id bigint,
    channel_id bigint
);
CREATE INDEX pinboard_pins_guild_idx ON public.pinboard_pins USING btree (guild_id);
CREATE UNIQUE INDEX pinboard_pins_message_idx ON public.pinboard_pins USING btree (message_id);

CREATE TABLE public.plonks (
    id serial NOT NULL,
    guild_id bigint,
    entity_id bigint,
    CONSTRAINT plonks_entity_id_key UNIQUE (entity_id),
    CONSTRAINT plonks_pkey PRIMARY KEY (id)
);
CREATE INDEX plonks_entity_id_idx ON public.plonks USING btree (entity_id);
CREATE INDEX plonks_guild_id_idx ON public.plonks USING btree (guild_id);

CREATE TABLE public.tags (
    id serial NOT NULL,
    guild_id bigint NOT NULL,
    name text NOT NULL,
    content text NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    author_id bigint NOT NULL,
    claimed boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    claimed_at timestamp with time zone,
    uses bigint DEFAULT 0 NOT NULL,
    CONSTRAINT tags_uses_check CHECK ((uses >= 0)),
    CONSTRAINT tags_pkey PRIMARY KEY (id)
);
CREATE INDEX tags_guild_author_idx ON public.tags USING btree (guild_id, author_id);
CREATE UNIQUE INDEX tags_guild_name_idx ON public.tags USING btree (guild_id, lower(name));
CREATE UNIQUE INDEX tags_id_unique_idx ON public.tags USING btree (id);

-- Reference data

CREATE TABLE public.added_pokemon (
    name text NOT NULL,
    created_at timestamp with time zone,
    CONSTRAINT added_pokemon_pkey PRIMARY KEY (name)
);

CREATE TABLE public.roblox_templates (
    asset_id bigint NOT NULL,
    image_url text NOT NULL,
    item_name text DEFAULT ''::text NOT NULL,
    extra jsonb DEFAULT '{}'::jsonb,
    cached_at timestamp with time zone DEFAULT now(),
    CONSTRAINT roblox_templates_pkey PRIMARY KEY (asset_id)
);

CREATE TABLE public.ror2_enemies (
    id serial NOT NULL,
    internal_name text NOT NULL,
    name text,
    type text,
    lore text,
    stats jsonb DEFAULT '{}'::jsonb,
    extra jsonb DEFAULT '{}'::jsonb,
    img_url text,
    CONSTRAINT ror2_enemies_pkey PRIMARY KEY (internal_name)
);

CREATE TABLE public.ror2_items (
    id serial NOT NULL,
    internal_name text NOT NULL,
    name text,
    desc_short text,
    desc_full text,
    rarity text,
    categories text[] DEFAULT '{}'::text[],
    achievement_locked text,
    stats jsonb DEFAULT '{}'::jsonb,
    lore text,
    desc_full_info text,
    corrupted_iname text,
    extra jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT ror2_items_pkey PRIMARY KEY (internal_name)
);
