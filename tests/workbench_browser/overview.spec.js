/* overview browser coverage, grouped by responsibility. */
const { test, DEPLOYMENTS } = require("./overview_support.js");
const sections = require("./overview_sections.js");
const filters = require("./overview_filters.js");
const observation = require("./overview_observation.js");
const history = require("./overview_history.js");

for (const deployment of DEPLOYMENTS) {
  test.describe(`${deployment.name} overview`, () => {
    sections(deployment);
    filters(deployment);
    observation(deployment);
    history(deployment);
  });
}
