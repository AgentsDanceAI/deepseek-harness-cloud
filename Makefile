.PHONY: check check-server check-desktop check-mobile check-deploy

check: check-server check-desktop check-mobile check-deploy

check-server:
	cd server && uv run ruff format --check app tests scripts
	cd server && uv run ruff check app tests scripts
	cd server && uv run pytest -q
	cd server && node --test tests/js/*.test.mjs

check-desktop:
	node desktop/scripts/verify-version.mjs
	node desktop/scripts/assemble.mjs
	cd desktop/build/upstream && corepack yarn install
	cd desktop/build/upstream && corepack yarn typecheck
	cd desktop/build/upstream && corepack yarn test --run

check-mobile:
	cd mobile && npm ci
	cd mobile && npm run check
	cd mobile && npx cap sync android
	cd mobile/android && ./gradlew test assembleDebug --no-daemon

check-deploy:
	docker compose -f deploy/selfhost/docker-compose.yml config --quiet
	server/.venv/bin/pytest tests/deploy tests/distribution tests/repository -q
