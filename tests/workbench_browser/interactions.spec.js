/* interactions browser coverage, grouped by responsibility. */
const { test, DEPLOYMENTS } = require("./interactions_support.js");
const capabilities = require("./interactions_capabilities.js");
const live_reply = require("./interactions_live_reply.js");
const mutations = require("./interactions_mutations.js");
const cleanup = require("./interactions_cleanup.js");
const detail = require("./interactions_detail.js");
const polling = require("./interactions_polling.js");
const archive = require("./interactions_archive.js");

for (const deployment of DEPLOYMENTS) {
  test.describe(`${deployment.name} deployment`, () => {
    capabilities(deployment);
    live_reply(deployment);
    mutations(deployment);
    cleanup(deployment);
    detail(deployment);
    polling(deployment);
    archive(deployment);
  });
}
