// ----- Request Statistics -----

// RequestStatus latency
requestStatus = from(bucket: "data")
  |> range(start: -5h)
  |> filter(fn: (r) => r["_measurement"] == "request_status")
  |> filter(fn: (r) => r["_field"] == "request_status")
  |> group(columns: ["request_id", "api_key", "model_key"], mode:"by")
  |> pivot(rowKey:["request_id"], columnKey: ["_value"], valueColumn: "_time")
  |> map(fn: (r) => ({ r with latency:  (float(v: r["4"]) - float(v: r["1"])) / float(v: 100000000) }))
  |> map(fn: (r) => ({ r with "comp_lt":  (float(v: r["4"]) - float(v: r["3"])) / float(v: 100000000) }))
  |> map(fn: (r) => ({ r with "run_lt":  (float(v: r["3"]) - float(v: r["2"])) / float(v: 100000000) }))
  |> map(fn: (r) => ({ r with "app_lt":  (float(v: r["2"]) - float(v: r["1"])) / float(v: 100000000) }))
  |> drop(columns: ["_start", "_stop", "1", "2", "3", "4"])

// Model Execution time latency
execTime = from(bucket: "data")
  |> range(start: -5h)
  |> filter(fn: (r) => r["_measurement"] == "request_execution_time")
  |> keep(columns: ["request_id", "_value"])
  |> rename(columns: {"_value": "exec_lt"})

requestStats = join(tables: {t1: requestStatus, t2: execTime}, on: ["request_id"])


// ----- Test Statistics -----

testResults = from(bucket: "TEST")
  |> range(start: -5h)
  |> drop(columns: ["_time", "_start", "_stop", "_field"])


// number of passed tests
sum = testResults
  |> group(columns: ["test_id"])
  |> sum()
  |> rename(columns: {"_value": "passed"})

// num_requests
count = testResults
  |> group(columns: ["test_id"])
  |> count()
  |> rename(columns: {"_value": "num_requests"})

// test parameters
testParams = testResults
  |> group(columns: ["test_id"])
  |> drop(columns: ["_value", "_field", "request_id"])
  |> unique(column: "test_id")

resultStats = join(tables: {t1: sum, t2: count}, on: ["test_id"])

testTable = join(tables: {t1: resultStats, t2: testParams}, on: ["test_id"])


// ----- Joined Table -----

joinedTable = join(
  tables: {t1: requestStats, t2: testResults},
  on: ["request_id"]
)
  |> group(columns: ["test_id"], mode: "by")


// ----- Statistics averaged per test_id -----

// APPROVED latency
approvedMean = joinedTable 
  |> mean(column: "app_lt") 
  |> map(fn: (r) => ({r with metric: "approved_mean"})) 
  |> rename(columns: {"app_lt": "_value"})

approvedStd = joinedTable
  |> stddev(column: "app_lt")
  |> map(fn: (r) => ({r with metric: "approved_std"})) 
  |> rename(columns: {"app_lt": "_value"})


// RUNNING latency
runningMean = joinedTable 
  |> mean(column: "run_lt") 
  |> map(fn: (r) => ({r with metric: "running_mean"})) 
  |> rename(columns: {"run_lt": "_value"})

runningStd = joinedTable 
  |> stddev(column: "run_lt") 
  |> map(fn: (r) => ({r with metric: "running_std"})) 
  |> rename(columns: {"run_lt": "_value"})


// COMPLETED latency
completedMean = joinedTable 
  |> mean(column: "comp_lt") 
  |> map(fn: (r) => ({r with metric: "completed_mean"})) 
  |> rename(columns: {"comp_lt": "_value"})

completedStd = joinedTable 
  |> stddev(column: "comp_lt") 
  |> map(fn: (r) => ({r with metric: "completed_std"})) 
  |> rename(columns: {"comp_lt": "_value"})


// Execution latency
executionMean = joinedTable 
  |> mean(column: "exec_lt") 
  |> map(fn: (r) => ({r with metric: "execution_mean"})) 
  |> rename(columns: {"exec_lt": "_value"})

executionStd = joinedTable 
  |> stddev(column: "exec_lt") 
  |> map(fn: (r) => ({r with metric: "execution_std"})) 
  |> rename(columns: {"exec_lt": "_value"})


// Total execution latency
latencyMean = joinedTable 
  |> mean(column: "latency") 
  |> map(fn: (r) => ({r with metric: "latency_mean"})) 
  |> rename(columns: {"latency": "_value"})

latencyStd = joinedTable 
  |> stddev(column: "latency") 
  |> map(fn: (r) => ({r with metric: "latency_std"})) 
  |> rename(columns: {"latency": "_value"})


// Joined metric table
metricTable = union(tables: [approvedMean, approvedStd, runningMean, runningStd, completedMean, completedStd, executionMean, executionStd, latencyMean, latencyStd])
  |> pivot(rowKey: ["test_id"], columnKey: ["metric"], valueColumn: "_value")


// ----- Final result -----

results = join(tables: {t1: testTable, t2: metricTable}, on: ["test_id"])
  |> rename(columns: {"_measurement": "test"})
  |> yield("name": "results")
